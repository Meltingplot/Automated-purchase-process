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
    stock_uom_mapping: dict | None = None,
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
        stock_uom_mapping=stock_uom_mapping,
    )
    if not items:
        frappe.throw("Cannot create Purchase Invoice without line items")

    # Retrospective documents must not be dated later than the source document
    doc_date = extracted_data.get("document_date") or today()
    due = extracted_data.get("delivery_date") or doc_date

    from .purchase_order import _build_taxes

    pi_data = {
        "doctype": "Purchase Invoice",
        "supplier": supplier,
        "company": settings.get("default_company"),
        "posting_date": doc_date,
        "due_date": due,
        "bill_no": extracted_data.get("document_number", ""),
        "bill_date": doc_date,
        "ai_retrospective": 1,
        "ai_procurement_job": job_name,
        "items": items,
    }

    # Set invoice currency — ERPNext auto-populates conversion_rate
    if extracted_data.get("currency"):
        pi_data["currency"] = extracted_data["currency"]

    taxes = _build_taxes(extracted_data, settings)
    if taxes:
        pi_data["taxes"] = taxes

    # Apply document-level discount (Rabatt/Skonto extracted from line items)
    if extracted_data.get("discount_amount"):
        pi_data["apply_discount_on"] = "Net Total"
        pi_data["discount_amount"] = extracted_data["discount_amount"]

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
    stock_uom_mapping: dict | None = None,
) -> list[dict]:
    """Build invoice items, optionally linked to PO and receipt with item-level links."""
    company = settings.get("default_company")
    items = []

    from .purchase_order import (
        _adjust_bulk_uom,
        _ensure_numeric_uom_setup,
        _resolve_item,
        _resolve_uom,
        _true_unit_price,
    )

    for idx, item in enumerate(extracted_data.get("items", [])):
        # Use item_code from linked PO/PR item when available — must match for ERPNext validation
        po_linked_code = po_item_links[idx]["item_code"] if po_item_links and idx in po_item_links else None
        pr_linked_code = pr_item_links[idx]["item_code"] if pr_item_links and idx in pr_item_links else None
        mapped_code = item_mapping.get(idx) if item_mapping else None
        item_code = po_linked_code or pr_linked_code or mapped_code or _resolve_item(item, settings, supplier, stock_uom=(stock_uom_mapping.get(idx) if stock_uom_mapping else None))
        qty = float(item.get("quantity", 1) or 1)
        rate = _true_unit_price(item, qty)
        uom_raw = item.get("uom") or ""
        _ensure_numeric_uom_setup(uom_raw)
        uom = _resolve_uom(uom_raw)
        qty, rate, uom = _adjust_bulk_uom(
            qty, rate, uom, item_code=item_code, currency=extracted_data.get("currency"),
        )
        _ensure_numeric_uom_setup(uom, item_code)

        invoice_item = {
            "item_code": item_code,
            "item_name": item.get("item_name", "Unknown Item"),
            "qty": qty,
            "rate": rate,
            "uom": uom,
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
