"""Item matching against ERPNext master data.

Matches extracted line items to existing ERPNext Item records
using supplier part number and item description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import frappe


@dataclass
class ItemMatch:
    """Result of an item matching attempt."""

    matched: bool
    item_code: str | None = None  # ERPNext Item Code
    item_name: str | None = None  # ERPNext Item Name
    confidence: float = 0.0
    method: str = ""
    candidates: list[dict] | None = None


def match_item(
    item_description: str,
    supplier_item_code: str | None = None,
    supplier_name: str | None = None,
) -> ItemMatch:
    """Match an extracted item to ERPNext Item records.

    Strategy:
    1. Supplier part number match (via Item Supplier child table)
    2. Item name/description fuzzy match
    3. No match → return for review

    Args:
        item_description: Item description from LLM extraction.
        supplier_item_code: Supplier's part number (optional).
        supplier_name: ERPNext Supplier name for scoped search (optional).

    Returns:
        ItemMatch with the result.
    """
    # Step 1: Match by supplier part number
    if supplier_item_code and supplier_name:
        part_match = _match_by_supplier_part_no(supplier_item_code, supplier_name)
        if part_match:
            return part_match

    # Also try without scoping to specific supplier
    if supplier_item_code:
        part_match = _match_by_supplier_part_no(supplier_item_code)
        if part_match:
            return part_match

    # Step 2: Fuzzy match by item name/description
    if item_description:
        fuzzy_match = _match_by_description(item_description)
        if fuzzy_match:
            return fuzzy_match

    return ItemMatch(matched=False, method="no_match")


def _match_by_supplier_part_no(
    part_no: str,
    supplier_name: str | None = None,
) -> ItemMatch | None:
    """Try to match by supplier's part/item number."""
    filters = {"supplier_part_no": part_no}
    if supplier_name:
        filters["supplier"] = supplier_name

    matches = frappe.get_all(
        "Item Supplier",
        filters=filters,
        fields=["parent", "supplier_part_no"],
        limit_page_length=5,
    )

    if not matches:
        return None

    if len(matches) == 1:
        item = frappe.get_value(
            "Item", matches[0]["parent"], ["name", "item_name"], as_dict=True
        )
        if item:
            return ItemMatch(
                matched=True,
                item_code=item["name"],
                item_name=item["item_name"],
                confidence=1.0,
                method="supplier_part_no",
            )

    # Multiple matches — return best guess + candidates
    candidates = []
    for m in matches:
        item = frappe.get_value(
            "Item", m["parent"], ["name", "item_name"], as_dict=True
        )
        if item:
            candidates.append(
                {"item_code": item["name"], "item_name": item["item_name"], "score": 0.9}
            )

    if candidates:
        return ItemMatch(
            matched=True,
            item_code=candidates[0]["item_code"],
            item_name=candidates[0]["item_name"],
            confidence=0.9,
            method="supplier_part_no_multiple",
            candidates=candidates,
        )

    return None


def _match_by_description(item_description: str) -> ItemMatch | None:
    """Fuzzy match by item name or description."""
    normalized = _normalize_description(item_description)

    all_items = frappe.get_all(
        "Item",
        fields=["name", "item_name", "description"],
        filters={"disabled": 0},
        limit_page_length=0,
    )

    best_score = 0.0
    best_item = None
    candidates = []

    for item in all_items:
        # Compare against both item_name and description
        score_name = _text_similarity(
            normalized, _normalize_description(item.get("item_name", ""))
        )
        score_desc = _text_similarity(
            normalized, _normalize_description(item.get("description", "") or "")
        )
        score = max(score_name, score_desc)

        if score > 0.5:
            candidates.append(
                {"item_code": item["name"], "item_name": item.get("item_name", ""), "score": score}
            )

        if score > best_score:
            best_score = score
            best_item = item

    candidates.sort(key=lambda x: x["score"], reverse=True)

    if best_score >= 0.75 and best_item:
        return ItemMatch(
            matched=True,
            item_code=best_item["name"],
            item_name=best_item.get("item_name", ""),
            confidence=best_score,
            method="fuzzy_description",
            candidates=candidates[:5],
        )

    if candidates:
        return ItemMatch(
            matched=False,
            confidence=best_score,
            method="low_confidence",
            candidates=candidates[:5],
        )

    return None


def _normalize_description(text: str) -> str:
    """Normalize item description for comparison."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _text_similarity(a: str, b: str) -> float:
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
