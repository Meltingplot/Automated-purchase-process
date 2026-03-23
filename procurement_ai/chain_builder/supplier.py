"""
Supplier matching and creation for the procurement chain.

Finds existing suppliers via fuzzy matching or creates new ones.
"""

from __future__ import annotations

import logging

import frappe
from frappe import _

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

    # Create Address if extracted data contains address info
    if data.get("supplier_address"):
        _create_supplier_address(supplier.name, data)

    logger.info(f"Created new supplier: {supplier.name}")
    return supplier.name


def _create_supplier_address(supplier_name: str, data: dict) -> None:
    """Create an Address linked to the supplier via Dynamic Link."""
    country = _detect_country(data)
    raw_address = data.get("supplier_address", "")

    # Parse address lines — use full text as address_line1,
    # extract city/pincode if possible
    address_line1, city, pincode = _parse_address(raw_address)

    address = frappe.get_doc(
        {
            "doctype": "Address",
            "address_title": data.get("supplier_name", supplier_name),
            "address_type": "Billing",
            "address_line1": address_line1 or raw_address[:140],
            "city": city or country,
            "country": country,
            "pincode": pincode or "",
            "email_id": data.get("supplier_email", ""),
            "phone": data.get("supplier_phone", ""),
            "links": [
                {
                    "link_doctype": "Supplier",
                    "link_name": supplier_name,
                }
            ],
        }
    )
    address.insert(ignore_permissions=True)
    logger.info(f"Created address {address.name} for supplier {supplier_name}")


def _parse_address(raw: str) -> tuple[str, str, str]:
    """
    Best-effort parse of a free-text address into components.

    Returns (address_line1, city, pincode). Any may be empty.
    """
    import re

    if not raw:
        return ("", "", "")

    lines = [line.strip() for line in raw.replace(",", "\n").split("\n") if line.strip()]

    address_line1 = lines[0] if lines else ""
    city = ""
    pincode = ""

    # Look for German-style "12345 City" pattern in remaining lines
    for line in lines[1:]:
        m = re.match(r"^(\d{4,5})\s+(.+)$", line)
        if m:
            pincode = m.group(1)
            city = m.group(2).strip()
            break

    # If no pincode pattern found, use last line as city
    if not city and len(lines) > 1:
        city = lines[-1]

    return (address_line1, city, pincode)


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
        _("No Supplier Group found. Please create at least one Supplier Group "
          "or set a default in Buying Settings.")
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
