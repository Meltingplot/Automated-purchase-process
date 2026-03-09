"""
Purchase Invoice creation from extracted data.
"""

from __future__ import annotations

import logging

import frappe
from frappe.utils import today

logger = logging.getLogger(__name__)


def create_purchase_invoice(
    extracted_data: dict,
    supplier: str,
    settings: dict,
    job_name: str,
    purchase_order: str | None = None,
    purchase_receipt: str | None = None,
    po_item_links: dict | None = None,
    pr_item_links: dict | None = None,
    item_mapping: dict | None = None,
) -> str:
    """
    Create a Purchase Invoice from extracted document data.

    Args:
        extracted_data: Consensus extraction data
        supplier: Supplier document name
        settings: Plugin settings dict
        job_name: AI Procurement Job name
        purchase_order: Optional linked PO name
        purchase_receipt: Optional linked PR name
        po_item_links: Optional mapping of extracted item index to PO item row
        pr_item_links: Optional mapping of extracted item index to PR item row

    Returns:
        Purchase Invoice name
    """
    items = _build_invoice_items(
        extracted_data,
        settings,
        supplier,
        purchase_order,
        purchase_receipt,
        po_item_links,
        pr_item_links,
        item_mapping=item_mapping,
    )
    if not items:
        frappe.throw("Cannot create Purchase Invoice without line items")

    pi_data = {
        "doctype": "Purchase Invoice",
        "supplier": supplier,
        "company": settings.get("default_company"),
        "posting_date": extracted_data.get("document_date") or today(),
        "due_date": extracted_data.get("delivery_date") or today(),
        "bill_no": extracted_data.get("document_number", ""),
        "bill_date": extracted_data.get("document_date") or today(),
        "ai_retrospective": 1,
        "ai_procurement_job": job_name,
        "items": items,
    }

    # Add tax charges from extracted data
    from .purchase_order import _build_taxes

    taxes = _build_taxes(extracted_data, settings)
    if taxes:
        pi_data["taxes"] = taxes

    pi = frappe.get_doc(pi_data)

    # Set payment terms if available
    if extracted_data.get("payment_terms"):
        pi.add_comment(
            "Comment",
            f"Payment terms from document: {extracted_data['payment_terms']}",
        )

    pi.insert(ignore_permissions=True)
    pi.add_comment(
        "Comment",
        f"Retrospectively created from {extracted_data.get('document_type', 'unknown')} "
        f"by AI Procurement (Job: {job_name})",
    )

    if settings.get("auto_submit_documents"):
        pi.submit()

    logger.info(f"Created Purchase Invoice: {pi.name}")
    return pi.name


def _build_invoice_items(
    extracted_data: dict,
    settings: dict,
    supplier: str,
    purchase_order: str | None,
    purchase_receipt: str | None,
    po_item_links: dict | None = None,
    pr_item_links: dict | None = None,
    item_mapping: dict | None = None,
) -> list[dict]:
    """Build invoice items, optionally linked to PO and receipt with item-level links."""
    company = settings.get("default_company")
    items = []

    from .purchase_order import _resolve_item, _resolve_uom

    for idx, item in enumerate(extracted_data.get("items", [])):
        mapped_code = item_mapping.get(idx) if item_mapping else None
        item_code = mapped_code if mapped_code else _resolve_item(item, settings, supplier)
        invoice_item = {
            "item_code": item_code,
            "item_name": item.get("item_name", "Unknown Item"),
            "qty": float(item.get("quantity", 1)),
            "rate": float(item.get("unit_price", 0)),
            "uom": _resolve_uom(item.get("uom", "Nos")),
            "expense_account": _get_default_expense_account(company),
        }

        if purchase_order:
            invoice_item["purchase_order"] = purchase_order
        if purchase_receipt:
            invoice_item["purchase_receipt"] = purchase_receipt

        # Link to specific PO item row if available
        if po_item_links and idx in po_item_links:
            invoice_item["po_detail"] = po_item_links[idx]["name"]

        # Link to specific PR item row if available
        if pr_item_links and idx in pr_item_links:
            invoice_item["pr_detail"] = pr_item_links[idx]["name"]

        items.append(invoice_item)

    return items


def _get_default_expense_account(company: str) -> str:
    """Get default expense account for the company."""
    # Try company default first
    if company:
        default = frappe.db.get_value("Company", company, "default_expense_account")
        if default:
            return default

    # Fall back to first non-group expense account for the company
    filters = {"root_type": "Expense", "is_group": 0}
    if company:
        filters["company"] = company

    accounts = frappe.get_all(
        "Account",
        filters=filters,
        fields=["name"],
        limit=1,
        order_by="creation asc",
    )
    if accounts:
        return accounts[0]["name"]

    frappe.throw(
        f"No expense account found for company {company!r}. "
        "Please set a default expense account in Company settings."
    )
