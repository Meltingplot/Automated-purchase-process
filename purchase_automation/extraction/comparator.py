"""Dual-model extraction result comparator.

Compares extraction results from two different LLM models field-by-field.
Determines whether results can be auto-accepted, need review, or must
be rejected based on configurable thresholds.

This is also a security layer: if one model is successfully manipulated
by a prompt injection, its output will differ from the unmanipulated
model, triggering automatic escalation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from purchase_automation.extraction.schemas import ExtractedDocument, ExtractedLineItem


class ComparisonLevel(str, Enum):
    """Escalation levels for dual-model comparison."""

    AUTO_ACCEPT = "auto_accept"  # Identical results
    AUTO_RESOLVE = "auto_resolve"  # Minor differences within tolerance
    REVIEW = "review"  # Significant differences, needs human review
    REJECT = "reject"  # Critical differences, manual entry required


@dataclass
class FieldComparison:
    """Comparison result for a single field."""

    field_name: str
    value_a: object
    value_b: object
    match: bool
    similarity: float = 1.0  # 0.0 to 1.0
    note: str = ""


@dataclass
class ComparisonResult:
    """Full comparison result between two extraction outputs."""

    level: ComparisonLevel
    overall_score: float  # 0.0 to 1.0
    field_comparisons: list[FieldComparison] = field(default_factory=list)
    merged_result: dict | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "overall_score": round(self.overall_score, 4),
            "summary": self.summary,
            "fields": [
                {
                    "field": fc.field_name,
                    "value_a": _safe_str(fc.value_a),
                    "value_b": _safe_str(fc.value_b),
                    "match": fc.match,
                    "similarity": round(fc.similarity, 4),
                    "note": fc.note,
                }
                for fc in self.field_comparisons
            ],
        }


def compare_extractions(
    result_a: ExtractedDocument,
    result_b: ExtractedDocument,
    *,
    auto_accept_threshold: float = 0.95,
    review_threshold: float = 0.70,
) -> ComparisonResult:
    """Compare two extraction results and determine escalation level.

    Args:
        result_a: Extraction from primary model.
        result_b: Extraction from secondary model.
        auto_accept_threshold: Score above which results are auto-accepted.
        review_threshold: Score below which results are rejected.

    Returns:
        ComparisonResult with escalation level and field-by-field details.
    """
    comparisons: list[FieldComparison] = []

    # --- Header fields ---
    comparisons.append(_compare_enum("document_type", result_a.document_type, result_b.document_type))
    comparisons.append(_compare_text("supplier_name", result_a.supplier_name, result_b.supplier_name))
    comparisons.append(_compare_optional_text("supplier_tax_id", result_a.supplier_tax_id, result_b.supplier_tax_id))
    comparisons.append(_compare_optional_text("document_number", result_a.document_number, result_b.document_number))
    comparisons.append(_compare_optional_date("document_date", result_a.document_date, result_b.document_date))
    comparisons.append(_compare_optional_date("delivery_date", result_a.delivery_date, result_b.delivery_date))
    comparisons.append(_compare_optional_date("due_date", result_a.due_date, result_b.due_date))
    comparisons.append(_compare_text("currency", result_a.currency, result_b.currency))

    # --- Totals (critical fields — exact match required) ---
    comparisons.append(_compare_amount("subtotal", result_a.subtotal, result_b.subtotal))
    comparisons.append(_compare_amount("tax_amount", result_a.tax_amount, result_b.tax_amount))
    comparisons.append(_compare_amount("total_amount", result_a.total_amount, result_b.total_amount))

    # --- Line items ---
    line_comparisons = _compare_line_items(result_a.line_items, result_b.line_items)
    comparisons.extend(line_comparisons)

    # --- Calculate overall score ---
    if not comparisons:
        overall_score = 0.0
    else:
        # Weight critical fields higher
        weighted_scores = []
        for fc in comparisons:
            weight = _field_weight(fc.field_name)
            weighted_scores.append((fc.similarity, weight))
        total_weight = sum(w for _, w in weighted_scores)
        if total_weight > 0:
            overall_score = sum(s * w for s, w in weighted_scores) / total_weight
        else:
            overall_score = 0.0

    # --- Determine escalation level ---
    has_critical_mismatch = any(
        not fc.match and _is_critical_field(fc.field_name) for fc in comparisons
    )

    if has_critical_mismatch:
        level = ComparisonLevel.REJECT
    elif overall_score >= auto_accept_threshold:
        level = ComparisonLevel.AUTO_ACCEPT
    elif overall_score >= review_threshold:
        level = ComparisonLevel.REVIEW
    else:
        level = ComparisonLevel.REJECT

    # If auto-accept or auto-resolve, merge results (prefer result_a)
    merged = None
    if level in (ComparisonLevel.AUTO_ACCEPT, ComparisonLevel.AUTO_RESOLVE):
        merged = result_a.model_dump(mode="json")

    mismatched = [fc for fc in comparisons if not fc.match]
    if mismatched:
        summary = (
            f"{len(mismatched)} field(s) differ: "
            + ", ".join(fc.field_name for fc in mismatched)
        )
    else:
        summary = "All fields match between both models."

    return ComparisonResult(
        level=level,
        overall_score=overall_score,
        field_comparisons=comparisons,
        merged_result=merged,
        summary=summary,
    )


# --- Field comparison helpers ---


def _compare_text(name: str, a: str, b: str) -> FieldComparison:
    """Compare two text fields with normalization."""
    na = _normalize_text(a)
    nb = _normalize_text(b)
    if na == nb:
        return FieldComparison(name, a, b, match=True, similarity=1.0)
    sim = _text_similarity(na, nb)
    return FieldComparison(
        name, a, b,
        match=sim >= 0.85,
        similarity=sim,
        note=f"Similarity: {sim:.2%}",
    )


def _compare_optional_text(name: str, a: str | None, b: str | None) -> FieldComparison:
    if a is None and b is None:
        return FieldComparison(name, a, b, match=True, similarity=1.0)
    if a is None or b is None:
        return FieldComparison(
            name, a, b, match=False, similarity=0.5,
            note="One model extracted value, the other did not",
        )
    return _compare_text(name, a, b)


def _compare_enum(name: str, a: object, b: object) -> FieldComparison:
    match = a == b
    return FieldComparison(name, a, b, match=match, similarity=1.0 if match else 0.0)


def _compare_optional_date(name: str, a, b) -> FieldComparison:
    if a is None and b is None:
        return FieldComparison(name, a, b, match=True, similarity=1.0)
    if a is None or b is None:
        return FieldComparison(
            name, a, b, match=False, similarity=0.5,
            note="One model extracted date, the other did not",
        )
    match = a == b
    return FieldComparison(name, str(a), str(b), match=match, similarity=1.0 if match else 0.0)


def _compare_amount(name: str, a: float | None, b: float | None) -> FieldComparison:
    """Compare monetary amounts with small tolerance for rounding."""
    if a is None and b is None:
        return FieldComparison(name, a, b, match=True, similarity=1.0)
    if a is None or b is None:
        return FieldComparison(
            name, a, b, match=False, similarity=0.5,
            note="One model extracted amount, the other did not",
        )
    diff = abs(a - b)
    if diff <= 0.01:
        return FieldComparison(name, a, b, match=True, similarity=1.0)
    # Relative difference
    max_val = max(abs(a), abs(b), 0.01)
    rel_diff = diff / max_val
    similarity = max(0.0, 1.0 - rel_diff)
    return FieldComparison(
        name, a, b,
        match=False,
        similarity=similarity,
        note=f"Difference: {diff:.2f} ({rel_diff:.2%})",
    )


def _compare_line_items(
    items_a: list[ExtractedLineItem],
    items_b: list[ExtractedLineItem],
) -> list[FieldComparison]:
    """Compare line item lists."""
    comparisons: list[FieldComparison] = []

    # First, compare count
    comparisons.append(FieldComparison(
        "line_item_count",
        len(items_a),
        len(items_b),
        match=len(items_a) == len(items_b),
        similarity=1.0 if len(items_a) == len(items_b) else 0.0,
        note="" if len(items_a) == len(items_b) else (
            f"Model A: {len(items_a)} items, Model B: {len(items_b)} items"
        ),
    ))

    # Compare paired items (by position)
    for i in range(min(len(items_a), len(items_b))):
        ia = items_a[i]
        ib = items_b[i]
        prefix = f"line_item[{i}]"

        comparisons.append(_compare_text(
            f"{prefix}.description", ia.item_description, ib.item_description
        ))
        comparisons.append(FieldComparison(
            f"{prefix}.quantity", ia.quantity, ib.quantity,
            match=ia.quantity == ib.quantity,
            similarity=1.0 if ia.quantity == ib.quantity else 0.0,
        ))
        comparisons.append(_compare_amount(
            f"{prefix}.unit_price", ia.unit_price, ib.unit_price
        ))
        comparisons.append(_compare_amount(
            f"{prefix}.total_price", ia.total_price, ib.total_price
        ))

    return comparisons


# --- Utility functions ---


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace, strip."""
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _text_similarity(a: str, b: str) -> float:
    """Calculate Jaccard token similarity between two strings."""
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
    return len(intersection) / len(union)


def _field_weight(field_name: str) -> float:
    """Return weight for a field (higher = more important for scoring)."""
    critical = {"total_amount", "subtotal", "tax_amount", "line_item_count"}
    high = {"supplier_name", "document_type", "currency"}
    quantity_pattern = re.compile(r"\.quantity$")
    price_pattern = re.compile(r"\.(unit_price|total_price)$")

    if field_name in critical or quantity_pattern.search(field_name):
        return 3.0
    if field_name in high or price_pattern.search(field_name):
        return 2.0
    return 1.0


def _is_critical_field(field_name: str) -> bool:
    """Check if a field mismatch should trigger automatic rejection."""
    critical = {"total_amount", "line_item_count"}
    return (
        field_name in critical
        or field_name.endswith(".quantity")
    )


def _safe_str(val: object) -> str:
    """Safely convert a value to string for serialization."""
    if val is None:
        return "null"
    return str(val)
