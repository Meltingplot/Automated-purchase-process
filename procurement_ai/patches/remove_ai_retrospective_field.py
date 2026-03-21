import frappe


def execute():
    """Remove deprecated ai_retrospective custom fields from PO/PR/PI."""
    for dt in ("Purchase Order", "Purchase Receipt", "Purchase Invoice"):
        name = f"{dt}-ai_retrospective"
        if frappe.db.exists("Custom Field", name):
            frappe.delete_doc("Custom Field", name, force=True)
