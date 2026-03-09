"""
Supplier matching and creation for the procurement chain.

Finds existing suppliers via fuzzy matching or creates new ones.
"""

from __future__ import annotations

import logging

import frappe

from ..validation.supplier_matcher import SupplierMatcher

logger = logging.getLogger(__name__)


def ensure_supplier(extracted_data: dict) -> str:
    """
    Find existing supplier or create a new one.

    Args:
        extracted_data: Consensus extraction data

    Returns:
        Supplier name (frappe document name)
    """
    match = SupplierMatcher.find_match(extracted_data)

    if match.found:
        logger.info(
            f"Matched supplier '{match.supplier_name}' "
            f"via {match.match_method} "
            f"(confidence: {match.match_confidence:.1%})"
        )
        return match.supplier_name

    # Create new supplier
    return _create_supplier(extracted_data)


def _create_supplier(data: dict) -> str:
    """Create a new Supplier document from extracted data."""
    supplier_name = data.get("supplier_name", "Unknown Supplier")

    supplier = frappe.get_doc(
        {
            "doctype": "Supplier",
            "supplier_name": supplier_name,
            "supplier_group": _get_default_supplier_group(),
            "supplier_type": "Company",
            "country": _detect_country(data),
        }
    )

    if data.get("supplier_tax_id"):
        supplier.tax_id = data["supplier_tax_id"]

    supplier.insert(ignore_permissions=True)
    supplier.add_comment(
        "Comment",
        "Automatically created by AI Procurement Plugin",
    )

    logger.info(f"Created new supplier: {supplier.name}")
    return supplier.name


def _get_default_supplier_group() -> str:
    """Get the default or first available Supplier Group."""
    # Try the ERPNext default buying settings first
    default = frappe.db.get_single_value("Buying Settings", "supplier_group")
    if default:
        return default

    # Fall back to first non-group Supplier Group
    groups = frappe.get_all(
        "Supplier Group",
        filters={"is_group": 0},
        fields=["name"],
        order_by="creation asc",
        limit=1,
    )
    if groups:
        return groups[0]["name"]

    # Last resort: first Supplier Group of any kind
    groups = frappe.get_all(
        "Supplier Group",
        fields=["name"],
        order_by="creation asc",
        limit=1,
    )
    if groups:
        return groups[0]["name"]

    frappe.throw(
        "No Supplier Group found. Please create at least one Supplier Group "
        "or set a default in Buying Settings."
    )


def _detect_country(data: dict) -> str:
    """Try to detect country from tax ID or address."""
    tax_id = data.get("supplier_tax_id", "")
    if tax_id.startswith("DE"):
        return "Germany"
    elif tax_id.startswith("AT"):
        return "Austria"
    elif tax_id.startswith("CH"):
        return "Switzerland"

    address = data.get("supplier_address", "").lower()
    if "deutschland" in address or "germany" in address:
        return "Germany"
    elif "österreich" in address or "austria" in address:
        return "Austria"
    elif "schweiz" in address or "switzerland" in address:
        return "Switzerland"

    return "Germany"  # Default
