import json

import frappe
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

        self.status = "Processing"
        self.save()
        frappe.db.commit()  # Release row lock before enqueuing

        frappe.enqueue(
            "erpnext_procurement_ai.procurement_ai.api.ingest.run_extraction_pipeline",
            queue="long",
            timeout=600,
            procurement_job_name=self.name,
        )

        frappe.msgprint(f"Processing started for {self.name}", alert=True)

    @frappe.whitelist()
    def approve_and_create(self, reviewed_data=None, item_mapping=None, stock_uom_mapping=None):
        """Approve reviewed data and trigger document chain creation.

        Accepts review data inline to avoid a separate save round-trip
        (two sequential writes to the same doc cause a deadlock).
        """
        if self.status != "Awaiting Review":
            frappe.throw(
                f"Cannot approve job in status '{self.status}'. "
                "Only jobs in 'Awaiting Review' can be approved."
            )

        if reviewed_data is not None:
            self.reviewed_data = reviewed_data if isinstance(reviewed_data, str) else json.dumps(reviewed_data)
        if item_mapping is not None:
            self.item_mapping = item_mapping if isinstance(item_mapping, str) else json.dumps(item_mapping)
        if stock_uom_mapping is not None:
            self.stock_uom_mapping = stock_uom_mapping if isinstance(stock_uom_mapping, str) else json.dumps(stock_uom_mapping)

        self.status = "Processing"
        self.save()
        frappe.db.commit()  # Release row lock before enqueuing

        frappe.enqueue(
            "erpnext_procurement_ai.procurement_ai.api.ingest.run_chain_from_review",
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
        if self.status != "Awaiting Review":
            frappe.throw(
                f"Cannot pre-create items in status '{self.status}'. "
                "Only jobs in 'Awaiting Review' can pre-create items."
            )

        data = json.loads(self.reviewed_data or self.consensus_data or "{}")
        if not data:
            frappe.throw("No extracted data available")

        from ....chain_builder.retrospective import sanitize_extracted_data
        from ....chain_builder.supplier import ensure_supplier
        from ....chain_builder.purchase_order import _resolve_item, _try_resolve_item

        clean = sanitize_extracted_data(data)
        supplier = ensure_supplier(clean)

        item_mapping = json.loads(self.item_mapping or "{}")
        stock_uom_mapping = json.loads(self.stock_uom_mapping or "{}")

        settings_doc = frappe.get_single("AI Procurement Settings")
        settings = settings_doc.get_settings_dict()

        results = []
        for idx, item in enumerate(clean.get("items", [])):
            mapped_code = item_mapping.get(str(idx))
            if mapped_code:
                results.append({
                    "idx": idx,
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

            existing = _try_resolve_item(item, settings, supplier)
            stock_uom = stock_uom_mapping.get(str(idx))
            item_code = _resolve_item(item, settings, supplier, stock_uom=stock_uom)

            results.append({
                "idx": idx,
                "item_code": item_code,
                "item_name": frappe.db.get_value(
                    "Item", item_code, "item_name",
                ) or item_code,
                "stock_uom": frappe.db.get_value(
                    "Item", item_code, "stock_uom",
                ),
                "created": existing is None,
            })

        frappe.db.commit()
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
        consensus = json.loads(self.consensus_data or "{}")
        if not consensus:
            return {"supplier": None, "items": []}

        # Sanitize data the same way build_chain does
        from ....chain_builder.retrospective import sanitize_extracted_data

        clean = sanitize_extracted_data(consensus)

        # Check supplier
        from ....validation.supplier_matcher import SupplierMatcher

        supplier_match = SupplierMatcher.find_match(clean)
        supplier_info = None
        if supplier_match.found:
            supplier_info = {
                "name": supplier_match.supplier_name,
                "method": supplier_match.match_method,
                "confidence": supplier_match.match_confidence,
            }

        # Check each item (try_resolve only, no creation) + UOM adjustment
        from ....chain_builder.purchase_order import (
            _adjust_bulk_uom,
            _resolve_uom,
            _true_unit_price,
            _try_resolve_item,
        )

        settings_doc = frappe.get_single("AI Procurement Settings")
        settings = settings_doc.get_settings_dict()
        supplier_name = supplier_match.supplier_name if supplier_match.found else ""
        invoice_currency = clean.get("currency")

        items_info = []
        for item in clean.get("items", []):
            matched = _try_resolve_item(item, settings, supplier_name)
            qty = float(item.get("quantity", 1) or 1)
            rate = _true_unit_price(item, qty)
            uom = _resolve_uom(item.get("uom", "Nos"))

            info = {
                "item_code": matched if matched else None,
                "exists": bool(matched),
                "resolved_uom": uom,
            }

            # Include stock UOM for existing items (can't be changed)
            if matched:
                info["stock_uom"] = frappe.db.get_value("Item", matched, "stock_uom")

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
