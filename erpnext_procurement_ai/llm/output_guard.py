"""
LLM output validation (Security Schicht 3).

Strict validation of LLM outputs against the Pydantic schema.
Prevents injected prompts from producing unexpected outputs.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from .schemas import ExtractedDocument


class OutputGuard:
    """
    Validates LLM output strictly against the expected schema.

    Pipeline:
    1. Extract JSON from LLM response (handles markdown code blocks)
    2. Validate as JSON object (not array, not string)
    3. Remove unexpected top-level keys
    4. Pydantic schema validation
    5. Plausibility checks (arithmetic validation)
    """

    @staticmethod
    def validate_extraction(
        raw_output: str,
    ) -> tuple[ExtractedDocument | None, list[str]]:
        """
        Validate raw LLM output and parse into ExtractedDocument.

        Returns:
            (parsed_document_or_none, list_of_errors)
        """
        errors: list[str] = []

        # 1. Extract JSON from response (handle markdown code blocks)
        json_str = OutputGuard._extract_json(raw_output)
        if json_str is None:
            return None, ["No valid JSON found in LLM output"]

        # 2. Parse JSON
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            return None, [f"Invalid JSON: {e}"]

        # 3. Must be a dict (not array, not string)
        if not isinstance(data, dict):
            return None, ["Output is not a JSON object"]

        # 4. Remove unexpected top-level keys
        allowed_keys = set(ExtractedDocument.model_fields.keys())
        unexpected = set(data.keys()) - allowed_keys
        if unexpected:
            errors.append(f"Unexpected fields removed: {unexpected}")
            data = {k: v for k, v in data.items() if k in allowed_keys}

        # 5. Pydantic validation
        try:
            result = ExtractedDocument.model_validate(data)
        except ValidationError as e:
            return None, [f"Schema validation failed: {e}"]

        # 6. Plausibility checks
        errors.extend(OutputGuard._plausibility_checks(result))

        return result, errors

    @staticmethod
    def _extract_json(text: str) -> str | None:
        """
        Extract JSON from LLM response.

        Handles:
        - Plain JSON
        - JSON wrapped in ```json ... ```
        - JSON wrapped in ``` ... ```
        """
        # Try to find JSON in markdown code blocks
        code_block_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL
        )
        if code_block_match:
            return code_block_match.group(1).strip()

        # Check if the outermost structure is a JSON array — reject it
        stripped = text.strip()
        if stripped.startswith("["):
            return None

        # Try to find raw JSON object
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            return brace_match.group(0)

        return None

    @staticmethod
    def _plausibility_checks(doc: ExtractedDocument) -> list[str]:
        """Run arithmetic and logical plausibility checks."""
        errors: list[str] = []

        # Sum check: line items vs total
        if doc.items and doc.total_amount:
            calc_total = sum(item.total_price for item in doc.items)
            if doc.shipping_cost:
                calc_total += doc.shipping_cost
            if doc.tax_amount:
                calc_total += doc.tax_amount

            diff = abs(float(calc_total) - float(doc.total_amount))
            if diff > 0.05:  # 5 cent tolerance
                errors.append(
                    f"Total mismatch: calculated={calc_total}, "
                    f"stated={doc.total_amount}, diff={diff:.2f}"
                )

        # Line item internal consistency
        for i, item in enumerate(doc.items):
            expected = float(item.quantity) * float(item.unit_price)
            if item.discount_percent:
                expected *= 1 - float(item.discount_percent) / 100
            actual = float(item.total_price)
            diff = abs(expected - actual)
            if diff > 0.05:
                errors.append(
                    f"Line item {i + 1} ({item.item_name}): "
                    f"qty*price={expected:.2f}, total={actual:.2f}, diff={diff:.2f}"
                )

        # Document type must be valid
        valid_types = {"cart", "order_confirmation", "delivery_note", "invoice"}
        if doc.document_type not in valid_types:
            errors.append(
                f"Invalid document_type '{doc.document_type}', "
                f"must be one of {valid_types}"
            )

        return errors
