"""Purchase Document controller.

Handles the lifecycle of an uploaded purchase document through
extraction, validation, review, and ERPNext document creation.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document


class PurchaseDocument(Document):

    def validate(self):
        if not self.source_file:
            frappe.throw(_("Please attach a source document."))

    def before_save(self):
        # Populate items table from merged_result if available
        if self.merged_result and not self.items:
            self._populate_items_from_result()

    def _populate_items_from_result(self):
        """Fill the items child table from the merged extraction result."""
        try:
            data = json.loads(self.merged_result)
        except (json.JSONDecodeError, TypeError):
            return

        line_items = data.get("line_items", [])
        for item in line_items:
            self.append("items", {
                "item_description": item.get("item_description", ""),
                "supplier_item_code": item.get("item_code_supplier"),
                "quantity": item.get("quantity", 0),
                "unit": item.get("unit"),
                "unit_price": item.get("unit_price"),
                "total_price": item.get("total_price"),
                "tax_rate": item.get("tax_rate"),
            })

    @frappe.whitelist()
    def reprocess(self):
        """Manually trigger re-extraction of this document."""
        self.status = "Uploaded"
        self.extraction_a_result = None
        self.extraction_b_result = None
        self.merged_result = None
        self.comparison_score = None
        self.comparison_details = None
        self.error_log = None
        self.items = []
        self.save(ignore_permissions=True)

        frappe.enqueue(
            "purchase_automation.orchestrator.workflow.process_document",
            queue="long",
            timeout=300,
            document_name=self.name,
        )

        frappe.msgprint(
            _("Re-extraction started in background."),
            alert=True,
        )

    @frappe.whitelist()
    def approve_and_create(self):
        """Approve the extracted data and create ERPNext documents."""
        if self.status not in ("Review", "Extracted"):
            frappe.throw(_("Can only approve documents in Review or Extracted status."))

        from purchase_automation.extraction.schemas import ExtractedDocument
        from purchase_automation.orchestrator.workflow import _create_erpnext_documents

        # Use merged result or primary extraction
        result_json = self.merged_result or self.extraction_a_result
        if not result_json:
            frappe.throw(_("No extraction result available."))

        data = json.loads(result_json)
        extracted = ExtractedDocument.model_validate(data)
        _create_erpnext_documents(self, extracted)
        self.save(ignore_permissions=True)
        frappe.db.commit()
