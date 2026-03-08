import frappe
from frappe.model.document import Document


class AIProcurementJob(Document):
    def before_save(self):
        if self.source_document and not self.source_document_url:
            self.source_document_url = self.source_document

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
