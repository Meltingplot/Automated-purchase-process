"""
Purchase Order creation from extracted data.
"""

from __future__ import annotations

import logging

import frappe
from frappe.utils import today

logger = logging.getLogger(__name__)


def create_purchase_order(
    extracted_data: dict,
    supplier: str,
    settings: dict,
    job_name: str,
) -> str:
    """
    Create a Purchase Order from extracted document data.

    Args:
        extracted_data: Consensus extraction data
        supplier: Supplier document name
        settings: Plugin settings dict
        job_name: AI Procurement Job name

    Returns:
        Purchase Order name
    """
    items = _build_items(extracted_data, settings)
    if not items:
        frappe.throw("Cannot create Purchase Order without line items")

    po = frappe.get_doc(
        {
            "doctype": "Purchase Order",
            "supplier": supplier,
            "company": settings.get("default_company"),
            "transaction_date": extracted_data.get("document_date") or today(),
            "schedule_date": extracted_data.get("delivery_date") or today(),
            "ai_retrospective": 1,
            "ai_procurement_job": job_name,
            "items": items,
        }
    )

    po.insert(ignore_permissions=True)
    po.add_comment(
        "Comment",
        f"Retrospectively created from {extracted_data.get('document_type', 'unknown')} "
        f"by AI Procurement (Job: {job_name})",
    )

    if settings.get("auto_submit_documents"):
        po.submit()

    logger.info(f"Created Purchase Order: {po.name}")
    return po.name


def _build_items(extracted_data: dict, settings: dict) -> list[dict]:
    """Build PO items list from extracted line items."""
    items = []
    schedule_date = extracted_data.get("delivery_date") or today()

    for item in extracted_data.get("items", []):
        item_code = _resolve_item(item, settings)
        items.append(
            {
                "item_code": item_code,
                "item_name": item.get("item_name", "Unknown Item"),
                "qty": float(item.get("quantity", 1)),
                "rate": float(item.get("unit_price", 0)),
                "uom": item.get("uom", "Nos"),
                "schedule_date": schedule_date,
            }
        )

    return items


def _resolve_item(item: dict, settings: dict) -> str:
    """
    Find or create an ERPNext Item matching the extracted item.

    Searches by item_name. Creates as Draft if not found.
    """
    item_name = item.get("item_name", "Unknown Item")

    # Search for existing item
    existing = frappe.get_all(
        "Item",
        filters={"item_name": ["like", f"%{item_name[:50]}%"]},
        fields=["name", "item_name"],
        limit=5,
    )

    if existing:
        return existing[0]["name"]

    # Search by item_code if provided
    if item.get("item_code"):
        existing = frappe.get_all(
            "Item",
            filters={"name": item["item_code"]},
            fields=["name"],
            limit=1,
        )
        if existing:
            return existing[0]["name"]

    # Create new item
    new_item = frappe.get_doc(
        {
            "doctype": "Item",
            "item_name": item_name,
            "item_group": "All Item Groups",
            "stock_uom": item.get("uom", "Nos"),
            "is_stock_item": 0,
            "description": item.get("description", item_name),
        }
    )
    new_item.insert(ignore_permissions=True)
    new_item.add_comment(
        "Comment",
        "Automatically created by AI Procurement Plugin",
    )

    logger.info(f"Created new Item: {new_item.name}")
    return new_item.name
