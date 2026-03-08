import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class AIEscalationLog(Document):
    def before_save(self):
        if self.status == "Resolved" and not self.resolved_at:
            self.resolved_at = now_datetime()
