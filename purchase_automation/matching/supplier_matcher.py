"""Supplier matching against ERPNext master data.

Matches an extracted supplier name to existing ERPNext Supplier records
using a multi-step strategy: exact match → fuzzy match → VAT ID lookup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import frappe


@dataclass
class SupplierMatch:
    """Result of a supplier matching attempt."""

    matched: bool
    supplier_name: str | None = None  # ERPNext Supplier name
    confidence: float = 0.0  # 0.0 to 1.0
    method: str = ""  # How the match was found
    candidates: list[dict] | None = None  # Alternative matches


def match_supplier(
    extracted_name: str,
    extracted_tax_id: str | None = None,
) -> SupplierMatch:
    """Match an extracted supplier name to ERPNext Supplier records.

    Strategy:
    1. Exact name match (case-insensitive)
    2. Fuzzy name match (token-based)
    3. VAT ID match (if available)
    4. No match → return for review

    Args:
        extracted_name: Supplier name from LLM extraction.
        extracted_tax_id: VAT ID from LLM extraction (optional).

    Returns:
        SupplierMatch with the result.
    """
    if not extracted_name:
        return SupplierMatch(matched=False, method="empty_name")

    normalized = _normalize_name(extracted_name)

    # Step 1: Exact match (case-insensitive)
    exact = frappe.db.get_value(
        "Supplier",
        {"supplier_name": ("like", extracted_name)},
        ["name", "supplier_name"],
        as_dict=True,
    )
    if exact:
        return SupplierMatch(
            matched=True,
            supplier_name=exact["name"],
            confidence=1.0,
            method="exact_name",
        )

    # Step 2: Fuzzy match against all suppliers
    all_suppliers = frappe.get_all(
        "Supplier",
        fields=["name", "supplier_name"],
        limit_page_length=0,
    )

    best_score = 0.0
    best_match = None
    candidates = []

    for supplier in all_suppliers:
        score = _name_similarity(normalized, _normalize_name(supplier["supplier_name"]))
        if score > 0.6:
            candidates.append(
                {"name": supplier["name"], "supplier_name": supplier["supplier_name"], "score": score}
            )
        if score > best_score:
            best_score = score
            best_match = supplier

    # Sort candidates by score
    candidates.sort(key=lambda x: x["score"], reverse=True)

    if best_score >= 0.85 and best_match:
        return SupplierMatch(
            matched=True,
            supplier_name=best_match["name"],
            confidence=best_score,
            method="fuzzy_name",
            candidates=candidates[:5],
        )

    # Step 3: VAT ID match
    if extracted_tax_id:
        clean_tax_id = re.sub(r"\s", "", extracted_tax_id)
        tax_match = frappe.db.get_value(
            "Supplier",
            {"tax_id": clean_tax_id},
            ["name", "supplier_name"],
            as_dict=True,
        )
        if tax_match:
            return SupplierMatch(
                matched=True,
                supplier_name=tax_match["name"],
                confidence=0.95,
                method="tax_id",
            )

    # No match found
    return SupplierMatch(
        matched=False,
        confidence=best_score,
        method="no_match",
        candidates=candidates[:5] if candidates else None,
    )


def _normalize_name(name: str) -> str:
    """Normalize a supplier name for comparison."""
    name = name.lower().strip()
    # Remove common suffixes
    for suffix in ("gmbh", "ag", "kg", "ohg", "e.k.", "mbh", "co.", "inc.", "ltd.", "llc"):
        name = name.replace(suffix, "")
    name = re.sub(r"[^\w\s]", " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def _name_similarity(a: str, b: str) -> float:
    """Token-based Jaccard similarity."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a and not tokens_b:
        return 1.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0
