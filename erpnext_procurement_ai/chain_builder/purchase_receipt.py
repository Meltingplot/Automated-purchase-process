"""
Purchase Receipt creation from extracted data.
"""

from __future__ import annotations

import logging

import frappe
from frappe.utils import today

logger = logging.getLogger(__name__)


def create_purchase_receipt(
    extracted_data: dict,
    supplier: str,
    settings: dict,
    job_name: str,
    purchase_order: str | None = None,
    po_item_links: dict | None = None,
    item_mapping: dict | None = None,
    stock_uom_mapping: dict | None = None,
) -> str:
    """
    Create a Purchase Receipt from extracted document data.

    Args:
        extracted_data: Consensus extraction data
        supplier: Supplier document name
        settings: Plugin settings dict
        job_name: AI Procurement Job name
        purchase_order: Optional linked Purchase Order name
        po_item_links: Optional mapping of extracted item index to PO item row
                       {index: {"name": row_name, "item_code": item_code}}

    Returns:
        Purchase Receipt name
    """
    items = _build_receipt_items(
        extracted_data, settings, supplier, purchase_order, po_item_links,
        item_mapping=item_mapping,
        stock_uom_mapping=stock_uom_mapping,
    )
    if not items:
        frappe.throw("Cannot create Purchase Receipt without line items")

    # Retrospective documents must not be dated later than the source document
    doc_date = extracted_data.get("document_date") or today()

    pr_data = {
        "doctype": "Purchase Receipt",
        "supplier": supplier,
        "company": settings.get("default_company"),
        "posting_date": doc_date,
        "ai_procurement_job": job_name,
        "items": items,
    }

    # Set invoice currency — ERPNext auto-populates conversion_rate
    if extracted_data.get("currency"):
        pr_data["currency"] = extracted_data["currency"]

    # Add tax charges (shipping + VAT) — same as PI so amounts match
    from .purchase_order import _build_taxes

    taxes = _build_taxes(extracted_data, settings)
    if taxes:
        pr_data["taxes"] = taxes

    # Apply document-level discount (Rabatt/Skonto extracted from line items)
    if extracted_data.get("discount_amount"):
        pr_data["apply_discount_on"] = "Net Total"
        pr_data["discount_amount"] = extracted_data["discount_amount"]

    pr = frappe.get_doc(pr_data)

    pr.insert(ignore_permissions=True)
    pr.add_comment(
        "Comment",
        f"Retrospectively created from {extracted_data.get('document_type', 'unknown')} "
        f"by AI Procurement (Job: {job_name})",
    )

    if settings.get("auto_submit_documents"):
        pr.submit()

    logger.info(f"Created Purchase Receipt: {pr.name}")
    return pr.name


def _build_receipt_items(
    extracted_data: dict,
    settings: dict,
    supplier: str,
    purchase_order: str | None,
    po_item_links: dict | None = None,
    item_mapping: dict | None = None,
    stock_uom_mapping: dict | None = None,
) -> list[dict]:
    """Build receipt items, optionally linked to a PO with item-level links."""
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
        # Use item_code from linked PO item when available — must match for ERPNext validation
        po_linked_code = po_item_links[idx]["item_code"] if po_item_links and idx in po_item_links else None
        mapped_code = item_mapping.get(idx) if item_mapping else None
        item_code = po_linked_code or mapped_code or _resolve_item(item, settings, supplier, stock_uom=(stock_uom_mapping.get(idx) if stock_uom_mapping else None))
        qty = float(item.get("quantity", 1) or 1)
        rate = _true_unit_price(item, qty)
        uom_raw = item.get("uom") or ""
        _ensure_numeric_uom_setup(uom_raw)
        uom = _resolve_uom(uom_raw)
        qty, rate, uom = _adjust_bulk_uom(
            qty, rate, uom, item_code=item_code, currency=extracted_data.get("currency"),
        )
        _ensure_numeric_uom_setup(uom, item_code)

        receipt_item = {
            "item_code": item_code,
            "item_name": item.get("item_name", "Unknown Item"),
            "qty": qty,
            "rate": rate,
            "uom": uom,
            "warehouse": _get_default_warehouse(company),
        }

        if purchase_order:
            receipt_item["purchase_order"] = purchase_order

        # Link to specific PO item row if available
        if po_item_links and idx in po_item_links:
            receipt_item["purchase_order_item"] = po_item_links[idx]["name"]

        items.append(receipt_item)

    return items


def _get_default_warehouse(company: str) -> str:
    """Get the default warehouse for receiving goods."""
    # Try Stock Settings default, but only if it belongs to the right company
    default = frappe.db.get_single_value("Stock Settings", "default_warehouse")
    if default:
        wh_company = frappe.db.get_value("Warehouse", default, "company")
        if not company or wh_company == company:
            return default

    # Fall back to first non-group warehouse for the company
    filters = {"is_group": 0}
    if company:
        filters["company"] = company

    warehouses = frappe.get_all(
        "Warehouse",
        filters=filters,
        fields=["name"],
        limit=1,
        order_by="creation asc",
    )
    if warehouses:
        return warehouses[0]["name"]

    frappe.throw(
        f"No warehouse found for company {company!r}. "
        "Please set a default warehouse in Company settings."
    )
