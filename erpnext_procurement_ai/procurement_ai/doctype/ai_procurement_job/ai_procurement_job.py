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
        if self.status not in ("Pending", "Error"):
            frappe.throw(f"Cannot process job in status '{self.status}'")

        self.status = "Processing"
        self.save()

        frappe.enqueue(
            "erpnext_procurement_ai.procurement_ai.api.ingest.run_extraction_pipeline",
            queue="long",
            timeout=600,
            procurement_job_name=self.name,
        )

        frappe.msgprint(f"Processing started for {self.name}", alert=True)

    @frappe.whitelist()
    def approve_and_create(self):
        """Approve reviewed data and trigger document chain creation."""
        if self.status != "Awaiting Review":
            frappe.throw(
                f"Cannot approve job in status '{self.status}'. "
                "Only jobs in 'Awaiting Review' can be approved."
            )

        self.status = "Processing"
        self.save()

        frappe.enqueue(
            "erpnext_procurement_ai.procurement_ai.api.ingest.run_chain_from_review",
            queue="long",
            timeout=600,
            procurement_job_name=self.name,
        )

        frappe.msgprint(f"Creating documents for {self.name}", alert=True)

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
        from ...chain_builder.retrospective import sanitize_extracted_data

        clean = sanitize_extracted_data(consensus)

        # Check supplier
        from ...validation.supplier_matcher import SupplierMatcher

        supplier_match = SupplierMatcher.find_match(clean)
        supplier_info = None
        if supplier_match.found:
            supplier_info = {
                "name": supplier_match.supplier_name,
                "method": supplier_match.match_method,
                "confidence": supplier_match.match_confidence,
            }

        # Check each item (try_resolve only, no creation)
        from ...chain_builder.purchase_order import _try_resolve_item

        settings_doc = frappe.get_single("AI Procurement Settings")
        settings = settings_doc.get_settings_dict()
        supplier_name = supplier_match.supplier_name if supplier_match.found else ""

        items_info = []
        for item in clean.get("items", []):
            matched = _try_resolve_item(item, settings, supplier_name)
            if matched:
                items_info.append({"item_code": matched, "exists": True})
            else:
                items_info.append({"item_code": None, "exists": False})

        return {"supplier": supplier_info, "items": items_info}
