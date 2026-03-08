"""
Fuzzy supplier matching against existing ERPNext suppliers.

Searches by name, tax ID, and email to find or create suppliers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.85  # 85% name similarity for a match


@dataclass
class SupplierMatch:
    """Result of a supplier matching attempt."""

    found: bool
    supplier_name: str | None = None
    match_confidence: float = 0.0
    match_method: str = ""  # "tax_id", "email", "name_fuzzy", "created"


class SupplierMatcher:
    """
    Finds existing suppliers or suggests creating new ones.

    Match priority:
    1. Exact tax ID match (highest confidence)
    2. Exact email match
    3. Fuzzy name match (>= 85% similarity)
    """

    @staticmethod
    def find_match(extracted_data: dict) -> SupplierMatch:
        """
        Search for a matching supplier in ERPNext.

        Args:
            extracted_data: Consensus extraction data containing
                supplier_name, supplier_tax_id, supplier_email

        Returns:
            SupplierMatch with result
        """
        import frappe

        supplier_name = extracted_data.get("supplier_name", "")
        tax_id = extracted_data.get("supplier_tax_id", "")
        email = extracted_data.get("supplier_email", "")

        # 1. Try exact tax ID match
        if tax_id:
            matches = frappe.get_all(
                "Supplier",
                filters={"tax_id": tax_id},
                fields=["name", "supplier_name"],
                limit=1,
            )
            if matches:
                return SupplierMatch(
                    found=True,
                    supplier_name=matches[0]["name"],
                    match_confidence=1.0,
                    match_method="tax_id",
                )

        # 2. Try email match via Dynamic Link on Address
        if email:
            # Check if email exists in supplier contact info
            suppliers = frappe.get_all(
                "Supplier",
                fields=["name", "supplier_name"],
                limit=100,
            )
            for s in suppliers:
                contacts = frappe.get_all(
                    "Dynamic Link",
                    filters={
                        "link_doctype": "Supplier",
                        "link_name": s["name"],
                        "parenttype": "Contact",
                    },
                    fields=["parent"],
                    limit=5,
                )
                for contact in contacts:
                    contact_doc = frappe.get_doc("Contact", contact["parent"])
                    for email_row in contact_doc.get("email_ids", []):
                        if email_row.email_id == email:
                            return SupplierMatch(
                                found=True,
                                supplier_name=s["name"],
                                match_confidence=0.95,
                                match_method="email",
                            )

        # 3. Fuzzy name match
        if supplier_name:
            all_suppliers = frappe.get_all(
                "Supplier",
                fields=["name", "supplier_name"],
                limit=500,
            )

            best_match = None
            best_score = 0.0

            for s in all_suppliers:
                score = SequenceMatcher(
                    None,
                    supplier_name.lower(),
                    s["supplier_name"].lower(),
                ).ratio()

                if score > best_score:
                    best_score = score
                    best_match = s

            if best_match and best_score >= MATCH_THRESHOLD:
                return SupplierMatch(
                    found=True,
                    supplier_name=best_match["name"],
                    match_confidence=best_score,
                    match_method="name_fuzzy",
                )

        # No match found
        return SupplierMatch(
            found=False,
            match_confidence=0.0,
            match_method="none",
        )
