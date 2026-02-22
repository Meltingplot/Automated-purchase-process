import frappe
from frappe.model.document import Document


class PurchaseAutomationSettings(Document):

    def validate(self):
        if self.auto_accept_threshold and (
            self.auto_accept_threshold < 0 or self.auto_accept_threshold > 1
        ):
            frappe.throw("Auto-Accept Threshold must be between 0.0 and 1.0")

        if self.review_threshold and (
            self.review_threshold < 0 or self.review_threshold > 1
        ):
            frappe.throw("Review Threshold must be between 0.0 and 1.0")

        if (
            self.auto_accept_threshold
            and self.review_threshold
            and self.auto_accept_threshold <= self.review_threshold
        ):
            frappe.throw(
                "Auto-Accept Threshold must be greater than Review Threshold"
            )

        # Warn if both providers use the same model
        if (
            self.primary_llm_provider == self.secondary_llm_provider
            and self.primary_model == self.secondary_model
        ):
            frappe.msgprint(
                "Primary and secondary models are identical. "
                "Dual-model validation is most effective with different models.",
                indicator="orange",
                alert=True,
            )
