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
) -> str:
    """
    Create a Purchase Receipt from extracted document data.

    Args:
        extracted_data: Consensus extraction data
        supplier: Supplier document name
        settings: Plugin settings dict
        job_name: AI Procurement Job name
        purchase_order: Optional linked Purchase Order name

    Returns:
        Purchase Receipt name
    """
    items = _build_receipt_items(extracted_data, settings, supplier, purchase_order)
    if not items:
        frappe.throw("Cannot create Purchase Receipt without line items")

    pr = frappe.get_doc(
        {
            "doctype": "Purchase Receipt",
            "supplier": supplier,
            "company": settings.get("default_company"),
            "posting_date": extracted_data.get("document_date") or today(),
            "ai_retrospective": 1,
            "ai_procurement_job": job_name,
            "items": items,
        }
    )

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
    extracted_data: dict, settings: dict, supplier: str, purchase_order: str | None
) -> list[dict]:
    """Build receipt items, optionally linked to a PO."""
    company = settings.get("default_company")
    items = []

    from .purchase_order import _resolve_item, _resolve_uom

    for item in extracted_data.get("items", []):
        item_code = _resolve_item(item, settings, supplier)
        receipt_item = {
            "item_code": item_code,
            "item_name": item.get("item_name", "Unknown Item"),
            "qty": float(item.get("quantity", 1)),
            "rate": float(item.get("unit_price", 0)),
            "uom": _resolve_uom(item.get("uom", "Nos")),
            "warehouse": _get_default_warehouse(company),
        }

        if purchase_order:
            receipt_item["purchase_order"] = purchase_order

        items.append(receipt_item)

    return items


def _get_default_warehouse(company: str) -> str:
    """Get the default warehouse for receiving goods."""
    # Try company default first
    if company:
        default = frappe.db.get_value("Company", company, "default_warehouse")
        if default:
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
