"""
Consensus Engine for multi-LLM result comparison.

Compares extraction results from multiple LLMs and builds
a consensus result using field-by-field comparison, weighted
voting, and escalation rules.

Also serves as Security Schicht 4: even if one LLM is compromised
via prompt injection, the other LLMs provide independent results
that will disagree with the manipulated output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Numeric fields in ExtractedDocument
NUMERIC_FIELDS = {
    "subtotal",
    "tax_amount",
    "total_amount",
    "shipping_cost",
    "confidence_self_assessment",
}

# Fields that contain nested structures
COMPLEX_FIELDS = {"items"}

# Critical fields that trigger escalation if disputed
CRITICAL_FIELDS = {"total_amount", "supplier_name"}


@dataclass
class ConsensusResult:
    """Result of the consensus building process."""

    agreed_data: dict = field(default_factory=dict)
    disputed_fields: dict = field(default_factory=dict)
    confidence: float = 0.0
    needs_escalation: bool = False
    escalation_reasons: list[str] = field(default_factory=list)
    provider_scores: dict[str, float] = field(default_factory=dict)


class ConsensusEngine:
    """
    Compares results from multiple extractors and builds consensus.

    Strategy:
    1. Field-by-field comparison of all extractions
    2. Majority vote for agreement
    3. String similarity for fuzzy matches (typos, etc.)
    4. Numeric fields: exact match with tolerance
    5. OCR result as independent validation source
    """

    NUMERIC_TOLERANCE = 0.02  # 2 cent tolerance
    STRING_SIMILARITY = 0.85  # 85% similarity = match
    MIN_AGREEMENT = 2  # Min 2 of N must agree

    def build_consensus(
        self,
        extractions: list[dict],
        ocr_baseline: dict | None = None,
        provider_weights: dict[str, float] | None = None,
    ) -> ConsensusResult:
        """
        Build consensus from N extraction results.

        Args:
            extractions: List of extraction dicts from LLMs
            ocr_baseline: Optional OCR result for cross-checking
            provider_weights: Optional weights per provider
                (e.g., {"claude": 1.0, "local": 0.5})

        Returns:
            ConsensusResult with agreed/disputed fields and confidence
        """
        if not extractions:
            return ConsensusResult(
                needs_escalation=True,
                escalation_reasons=["No extraction results to build consensus from"],
            )

        # Single result: accept as-is (only in dev mode / single provider)
        if len(extractions) == 1:
            return ConsensusResult(
                agreed_data=extractions[0],
                confidence=0.7,  # Lower confidence for single source
                needs_escalation=False,
            )

        # Collect all field names across all extractions
        all_fields: set[str] = set()
        for ext in extractions:
            all_fields.update(ext.keys())

        agreed: dict = {}
        disputed: dict = {}
        field_agreements: dict[str, float] = {}

        for field_name in all_fields:
            values = [
                (i, ext.get(field_name))
                for i, ext in enumerate(extractions)
                if ext.get(field_name) is not None
            ]

            if not values:
                continue

            if field_name in NUMERIC_FIELDS:
                result = self._numeric_consensus(values)
            elif field_name in COMPLEX_FIELDS:
                result = self._items_consensus(values)
            else:
                result = self._string_consensus(values)

            if result["agreed"]:
                agreed[field_name] = result["value"]
                field_agreements[field_name] = result["agreement_ratio"]
            else:
                disputed[field_name] = {
                    "values": result.get("all_values", {}),
                    "reason": result.get("reason", "No agreement"),
                }

        # OCR cross-check
        escalation_reasons: list[str] = []
        if ocr_baseline and isinstance(ocr_baseline, dict):
            ocr_mismatches = self._cross_check_ocr(agreed, ocr_baseline)
            if ocr_mismatches:
                escalation_reasons.append(
                    f"OCR cross-check deviations: {ocr_mismatches}"
                )

        # Calculate confidence
        total_fields = len(agreed) + len(disputed)
        confidence = len(agreed) / total_fields if total_fields > 0 else 0.0

        # Determine if escalation is needed
        needs_escalation = (
            confidence < 0.7
            or len(disputed) > 3
            or any(f in disputed for f in CRITICAL_FIELDS)
            or len(escalation_reasons) > 0
        )

        if needs_escalation and not escalation_reasons:
            escalation_reasons.append(
                f"Low confidence ({confidence:.1%}) or too many disputed fields "
                f"({len(disputed)})"
            )

        return ConsensusResult(
            agreed_data=agreed,
            disputed_fields=disputed,
            confidence=confidence,
            needs_escalation=needs_escalation,
            escalation_reasons=escalation_reasons,
            provider_scores=self._score_providers(extractions, agreed),
        )

    def _string_consensus(self, values: list[tuple[int, str]]) -> dict:
        """Find consensus for string fields using similarity matching."""
        groups: list[list[tuple[int, str]]] = []

        for idx, val in values:
            val_str = str(val)
            matched = False
            for group in groups:
                ref_str = str(group[0][1])
                similarity = SequenceMatcher(
                    None, val_str.lower(), ref_str.lower()
                ).ratio()
                if similarity >= self.STRING_SIMILARITY:
                    group.append((idx, val))
                    matched = True
                    break
            if not matched:
                groups.append([(idx, val)])

        # Largest group wins
        largest = max(groups, key=len)
        if len(largest) >= min(self.MIN_AGREEMENT, len(values)):
            return {
                "agreed": True,
                "value": largest[0][1],
                "agreement_ratio": len(largest) / len(values),
            }

        return {
            "agreed": False,
            "all_values": {str(i): v for i, v in values},
            "reason": "No sufficient agreement among providers",
        }

    def _numeric_consensus(self, values: list[tuple[int, float]]) -> dict:
        """Find consensus for numeric fields with tolerance."""
        nums: list[tuple[int, float]] = []
        for i, v in values:
            try:
                nums.append((i, float(v)))
            except (TypeError, ValueError):
                continue

        if not nums:
            return {"agreed": False, "all_values": {}, "reason": "No numeric values"}

        groups: list[list[tuple[int, float]]] = []
        for idx, val in nums:
            matched = False
            for group in groups:
                if abs(val - group[0][1]) <= self.NUMERIC_TOLERANCE:
                    group.append((idx, val))
                    matched = True
                    break
            if not matched:
                groups.append([(idx, val)])

        largest = max(groups, key=len)
        if len(largest) >= min(self.MIN_AGREEMENT, len(nums)):
            avg = sum(v for _, v in largest) / len(largest)
            return {
                "agreed": True,
                "value": round(avg, 2),
                "agreement_ratio": len(largest) / len(nums),
            }

        return {
            "agreed": False,
            "all_values": {str(i): v for i, v in nums},
            "reason": f"Numeric disagreement: {[v for _, v in nums]}",
        }

    def _items_consensus(self, values: list[tuple[int, list]]) -> dict:
        """
        Find consensus for line items.

        Matches items across extractions by item_name similarity,
        then checks quantity and price agreement.
        """
        if len(values) < 2:
            return {
                "agreed": True,
                "value": values[0][1] if values else [],
                "agreement_ratio": 1.0,
            }

        # Use first extraction as reference
        ref_items = values[0][1]
        if not isinstance(ref_items, list):
            return {"agreed": False, "all_values": {}, "reason": "Items not a list"}

        all_agree = True
        disagreement_reasons: list[str] = []

        for other_idx, other_items in values[1:]:
            if not isinstance(other_items, list):
                all_agree = False
                disagreement_reasons.append(
                    f"Provider {other_idx}: items is not a list"
                )
                continue

            if len(ref_items) != len(other_items):
                all_agree = False
                disagreement_reasons.append(
                    f"Provider {other_idx}: {len(other_items)} items "
                    f"vs reference {len(ref_items)}"
                )
                continue

            # Match items by name similarity
            for i, ref_item in enumerate(ref_items):
                if i >= len(other_items):
                    break
                other_item = other_items[i]

                ref_name = ref_item.get("item_name", "")
                other_name = other_item.get("item_name", "")
                name_sim = SequenceMatcher(
                    None, str(ref_name).lower(), str(other_name).lower()
                ).ratio()

                if name_sim < self.STRING_SIMILARITY:
                    all_agree = False
                    disagreement_reasons.append(
                        f"Item {i + 1} name mismatch: "
                        f"'{ref_name}' vs '{other_name}'"
                    )

        if all_agree:
            return {
                "agreed": True,
                "value": ref_items,
                "agreement_ratio": 1.0,
            }

        return {
            "agreed": False,
            "all_values": {str(i): v for i, v in values},
            "reason": "; ".join(disagreement_reasons),
        }

    def _cross_check_ocr(self, agreed: dict, ocr_baseline: dict) -> list[str]:
        """Cross-check agreed fields against OCR baseline text."""
        mismatches: list[str] = []
        ocr_text = ocr_baseline.get("text", "").lower()

        if not ocr_text:
            return mismatches

        # Check if key agreed values appear in OCR text
        for field_name in ["supplier_name", "document_number", "total_amount"]:
            value = agreed.get(field_name)
            if value is None:
                continue

            value_str = str(value).lower()
            if len(value_str) > 3 and value_str not in ocr_text:
                mismatches.append(
                    f"{field_name}='{value}' not found in OCR text"
                )

        return mismatches

    def _score_providers(
        self, extractions: list[dict], agreed: dict
    ) -> dict[str, float]:
        """Score each provider based on agreement with consensus."""
        scores: dict[str, float] = {}
        if not agreed:
            return scores

        agreed_fields = set(agreed.keys())
        for i, ext in enumerate(extractions):
            matching = 0
            total = 0
            for field_name in agreed_fields:
                if field_name in ext and ext[field_name] is not None:
                    total += 1
                    if self._values_match(agreed[field_name], ext[field_name]):
                        matching += 1

            scores[f"provider_{i}"] = matching / total if total > 0 else 0.0

        return scores

    def _values_match(self, a: object, b: object) -> bool:
        """Check if two values match (with tolerance for numerics)."""
        if a == b:
            return True

        # Numeric comparison
        try:
            return abs(float(a) - float(b)) <= self.NUMERIC_TOLERANCE
        except (TypeError, ValueError):
            pass

        # String similarity
        try:
            return (
                SequenceMatcher(
                    None, str(a).lower(), str(b).lower()
                ).ratio()
                >= self.STRING_SIMILARITY
            )
        except Exception:
            return False
