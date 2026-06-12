import json

import frappe
from frappe import _
from frappe.model.document import Document


class AIProcurementJob(Document):
    def before_save(self):
        if self.source_document and not self.source_document_url:
            self.source_document_url = self.source_document

    def on_trash(self):
        """Clear back-references on linked documents so deletion is not blocked."""
        for doctype in ("Purchase Order", "Purchase Receipt", "Purchase Invoice"):
            linked = frappe.get_all(
                doctype,
                filters={"ai_procurement_job": self.name},
                fields=["name"],
            )
            for doc in linked:
                frappe.db.set_value(
                    doctype, doc["name"], "ai_procurement_job", None,
                    update_modified=False,
                )

    @frappe.whitelist()
    def process_document(self):
        """Trigger document processing via background job."""
        if self.status not in ("Pending", "Error", "Needs Review"):
            frappe.throw(f"Cannot process job in status '{self.status}'")

        # Verify the user has permissions to create all document types
        from ...api.ingest import _check_creation_permissions

        _check_creation_permissions()

        self.status = "Processing"
        self.save()
        frappe.db.commit()  # Release row lock before enqueuing  # nosemgrep

        frappe.enqueue(
            "procurement_ai.procurement_ai.api.ingest.run_extraction_pipeline",
            queue="long",
            timeout=600,
            procurement_job_name=self.name,
        )

        frappe.msgprint(f"Processing started for {self.name}", alert=True)

    @frappe.whitelist()
    def approve_and_create(self, reviewed_data: str | None = None, item_mapping: str | None = None, stock_uom_mapping: str | None = None, supplier_mapping: str | None = None):
        """Approve reviewed data and trigger document chain creation.

        Accepts review data inline to avoid a separate save round-trip
        (two sequential writes to the same doc cause a deadlock).
        """
        if self.status not in ("Awaiting Review", "Needs Review"):
            frappe.throw(
                f"Cannot approve job in status '{self.status}'. "
                "Only jobs in 'Awaiting Review' or 'Needs Review' can be approved."
            )

        # Verify the job owner has permissions to create all document types
        from ...api.ingest import _check_creation_permissions

        _check_creation_permissions(user=self.owner)

        if reviewed_data is not None:
            # Validate reviewed_data against the extraction schema
            from pydantic import ValidationError as PydanticValidationError

            from ....llm.schemas import ExtractedDocument

            data = json.loads(reviewed_data) if isinstance(reviewed_data, str) else reviewed_data
            try:
                ExtractedDocument.model_validate(data)
            except PydanticValidationError as e:
                problems = []
                for err in e.errors():
                    loc = " → ".join(str(p) for p in err.get("loc", ())) or _("document")
                    problems.append(
                        "<li><b>{0}</b>: {1}</li>".format(
                            frappe.utils.escape_html(loc),
                            frappe.utils.escape_html(err.get("msg", "")),
                        )
                    )
                frappe.throw(
                    _("The reviewed data is incomplete or invalid:")
                    + "<ul>{0}</ul>".format("".join(problems))
                    + _("Please correct these fields in the review form and try again."),
                    title=_("Validation Error"),
                )

            self.reviewed_data = reviewed_data if isinstance(reviewed_data, str) else json.dumps(reviewed_data, ensure_ascii=False)
        if item_mapping is not None:
            self.item_mapping = item_mapping if isinstance(item_mapping, str) else json.dumps(item_mapping, ensure_ascii=False)
        if stock_uom_mapping is not None:
            self.stock_uom_mapping = stock_uom_mapping if isinstance(stock_uom_mapping, str) else json.dumps(stock_uom_mapping, ensure_ascii=False)
        if supplier_mapping is not None:
            # Validate the assigned supplier exists before storing
            supplier_mapping = supplier_mapping.strip()
            if supplier_mapping and not frappe.db.exists("Supplier", supplier_mapping):
                frappe.throw(
                    _("Assigned supplier '{0}' does not exist.").format(supplier_mapping),
                    title=_("Invalid Supplier"),
                )
            self.supplier_mapping = supplier_mapping or None

        self.status = "Processing"
        self.save()
        frappe.db.commit()  # Release row lock before enqueuing  # nosemgrep

        frappe.enqueue(
            "procurement_ai.procurement_ai.api.ingest.run_chain_from_review",
            queue="long",
            timeout=600,
            procurement_job_name=self.name,
        )

        frappe.msgprint(f"Creating documents for {self.name}", alert=True)

    @frappe.whitelist()
    def precreate_items(self):
        """Pre-create Items so the user can edit them before approving.

        For each line item not already mapped via the Link control,
        finds an existing Item or creates a new one. Returns a list
        of {idx, item_code, item_name, stock_uom, created} dicts
        so the review UI can update badges and lock controls.
        """
        if self.status not in ("Awaiting Review", "Needs Review"):
            frappe.throw(
                f"Cannot pre-create items in status '{self.status}'. "
                "Only jobs in 'Awaiting Review' or 'Needs Review' can pre-create items."
            )

        if not frappe.has_permission("Item", ptype="create"):
            frappe.throw(
                _("You do not have permission to create Items."),
                frappe.PermissionError,
            )

        data = json.loads(self.reviewed_data or self.consensus_data or "{}")
        if not data:
            frappe.throw(_("No extracted data available"))

        from ....chain_builder.retrospective import sanitize_extracted_data
        from ....chain_builder.supplier import ensure_supplier
        from ....chain_builder.purchase_order import (
            _create_item,
            _resolve_item,
            _try_resolve_item,
        )

        clean = sanitize_extracted_data(data)
        supplier = ensure_supplier(clean, forced_supplier=self.supplier_mapping or None)

        item_mapping = json.loads(self.item_mapping or "{}")
        stock_uom_mapping = json.loads(self.stock_uom_mapping or "{}")

        settings_doc = frappe.get_single("AI Procurement Settings")
        settings = settings_doc.get_settings_dict()
        if self.company:
            settings["default_company"] = self.company

        results = []
        for idx, item in enumerate(clean.get("items", [])):
            # Mappings + UI rows are keyed by the review-UI row index;
            # sanitization may have removed rows (shipping/discount), so use
            # the original index stamped by sanitize_extracted_data.
            ui_idx = item.get("_orig_idx", idx)
            mapped_code = item_mapping.get(str(ui_idx))
            # A key present with a falsy value means the user explicitly cleared
            # the mapping → force creation of a new Item (skip fuzzy matching),
            # mirroring the user_cleared logic in purchase_order._build_items.
            user_cleared = str(ui_idx) in item_mapping and not mapped_code
            stock_uom = stock_uom_mapping.get(str(ui_idx))
            if mapped_code:
                results.append({
                    "idx": ui_idx,
                    "item_code": mapped_code,
                    "item_name": frappe.db.get_value(
                        "Item", mapped_code, "item_name",
                    ) or mapped_code,
                    "stock_uom": frappe.db.get_value(
                        "Item", mapped_code, "stock_uom",
                    ),
                    "created": False,
                })
                continue

            if user_cleared:
                item_code = _create_item(item, supplier, settings, stock_uom=stock_uom)
                created = True
            else:
                existing = _try_resolve_item(item, settings, supplier)
                item_code = _resolve_item(item, settings, supplier, stock_uom=stock_uom)
                created = existing is None

            results.append({
                "idx": ui_idx,
                "item_code": item_code,
                "item_name": frappe.db.get_value(
                    "Item", item_code, "item_name",
                ) or item_code,
                "stock_uom": frappe.db.get_value(
                    "Item", item_code, "stock_uom",
                ),
                "created": created,
            })

        frappe.db.commit()  # Persist newly created Items before returning to client  # nosemgrep
        return results

    @frappe.whitelist()
    def mark_completed(self):
        """Mark the job as completed after user has verified created documents."""
        if self.status != "Needs Review":
            frappe.throw(
                f"Cannot mark job as completed in status '{self.status}'. "
                "Only jobs in 'Needs Review' can be marked completed."
            )

        self.status = "Completed"
        self.save()
        frappe.msgprint(f"Job {self.name} marked as completed", alert=True)

    @frappe.whitelist()
    def check_review_matches(self):
        """Check which supplier/items already exist vs. would be created.

        Returns dict with supplier match info and per-item match info,
        used by the review UI to show "exists" / "will create" badges.
        """
        # Use reviewed data when present so badges/indices match the rendered
        # review UI (rows may have been edited, added or deleted by the user).
        consensus = json.loads(self.reviewed_data or self.consensus_data or "{}")
        if not consensus:
            return {"supplier": None, "items": []}

        # Sanitize data the same way build_chain does
        from ....chain_builder.retrospective import sanitize_extracted_data

        clean = sanitize_extracted_data(consensus)

        # Check supplier
        from ....validation.supplier_matcher import SupplierMatcher

        forced_supplier = self.supplier_mapping or None
        if forced_supplier and frappe.db.exists("Supplier", forced_supplier):
            supplier_info = {
                "name": forced_supplier,
                "method": "assigned",
                "confidence": 1.0,
            }
            resolved_supplier_name = forced_supplier
        else:
            supplier_match = SupplierMatcher.find_match(clean)
            supplier_info = None
            resolved_supplier_name = ""
            if supplier_match.found:
                supplier_info = {
                    "name": supplier_match.supplier_name,
                    "method": supplier_match.match_method,
                    "confidence": supplier_match.match_confidence,
                }
                resolved_supplier_name = supplier_match.supplier_name

        # Check each item (try_resolve only, no creation) + UOM adjustment
        from ....chain_builder.purchase_order import (
            _adjust_bulk_uom,
            _get_piece_uom,
            _is_numeric_uom,
            _resolve_uom,
            _true_unit_price,
            _try_resolve_item,
        )

        settings_doc = frappe.get_single("AI Procurement Settings")
        settings = settings_doc.get_settings_dict()
        if self.company:
            settings["default_company"] = self.company
        supplier_name = resolved_supplier_name
        invoice_currency = clean.get("currency")

        items_info = []
        for pos, item in enumerate(clean.get("items", [])):
            matched = _try_resolve_item(item, settings, supplier_name)
            qty = float(item.get("quantity", 1) or 1)
            rate = _true_unit_price(item, qty)
            uom_raw = str(item.get("uom") or "Nos")

            info = {
                # Review-UI row index — sanitization may have removed rows
                # (shipping/discount), so position and UI index can differ.
                "idx": item.get("_orig_idx", pos),
                "item_code": matched if matched else None,
                "exists": bool(matched),
            }

            # Include stock UOM for existing items (can't be changed)
            if matched:
                info["stock_uom"] = frappe.db.get_value("Item", matched, "stock_uom")

            if _is_numeric_uom(uom_raw):
                # Package line resolved to a numeric bulk UOM during
                # sanitization ("1 VPE à 200 Stück" → uom "200"). The UOM
                # record may not exist yet (it is created at chain-build
                # time), so _resolve_uom would fall back to 1 piece here.
                # Resolve to the piece UOM for the UI control and prefill
                # the stock detail with the contained piece count instead.
                factor = float(uom_raw)
                info["resolved_uom"] = _get_piece_uom()
                info["uom_adjustment"] = {
                    "original_qty": qty * factor,  # pieces into stock
                    "suggested_doc_qty": qty,  # packages on the document line
                    "original_rate": rate / factor if factor else rate,
                    "adjusted_rate": rate,  # price per package
                }
                # Tell the UI when the bulk UOM record does not exist yet —
                # it is auto-created at chain-build time.
                if not frappe.db.exists("UOM", uom_raw):
                    info["uom_will_be_created"] = uom_raw
            else:
                uom = _resolve_uom(uom_raw)
                info["resolved_uom"] = uom

                # Check if bulk UOM adjustment would apply
                adj_qty, adj_rate, adj_uom = _adjust_bulk_uom(
                    qty, rate, uom, item_code=matched, currency=invoice_currency,
                    dry_run=True,
                )
                if adj_uom != uom:
                    info["uom_adjustment"] = {
                        "original_qty": qty,
                        "suggested_doc_qty": adj_qty,
                        "original_rate": rate,
                        "adjusted_rate": adj_rate,
                    }

            items_info.append(info)

        return {"supplier": supplier_info, "items": items_info}
