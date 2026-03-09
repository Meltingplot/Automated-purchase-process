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
