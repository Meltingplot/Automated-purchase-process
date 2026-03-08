"""Tests for OutputGuard - LLM output validation."""

from __future__ import annotations

import json

import pytest

from erpnext_procurement_ai.llm.output_guard import OutputGuard


class TestOutputGuard:
    """Test suite for OutputGuard."""

    def test_valid_json_extraction(self, sample_extraction_json):
        result, errors = OutputGuard.validate_extraction(sample_extraction_json)
        assert result is not None
        assert result.supplier_name == "ACME GmbH"
        assert result.document_type == "invoice"
        assert len(result.items) == 2

    def test_json_in_markdown_block(self, sample_extraction):
        wrapped = f"```json\n{json.dumps(sample_extraction)}\n```"
        result, errors = OutputGuard.validate_extraction(wrapped)
        assert result is not None
        assert result.supplier_name == "ACME GmbH"

    def test_json_in_plain_code_block(self, sample_extraction):
        wrapped = f"```\n{json.dumps(sample_extraction)}\n```"
        result, errors = OutputGuard.validate_extraction(wrapped)
        assert result is not None

    def test_json_with_surrounding_text(self, sample_extraction):
        wrapped = f"Here is the extraction:\n{json.dumps(sample_extraction)}\nDone."
        result, errors = OutputGuard.validate_extraction(wrapped)
        assert result is not None

    def test_invalid_json(self):
        result, errors = OutputGuard.validate_extraction("this is not json")
        assert result is None
        assert len(errors) > 0

    def test_json_array_rejected(self):
        result, errors = OutputGuard.validate_extraction('[{"a": 1}]')
        assert result is None
        assert len(errors) > 0

    def test_missing_required_fields(self):
        result, errors = OutputGuard.validate_extraction('{"document_type": "invoice"}')
        assert result is None
        assert any("validation failed" in e.lower() for e in errors)

    def test_unexpected_fields_stripped(self, sample_extraction):
        data = {**sample_extraction, "malicious_field": "should be removed"}
        result, errors = OutputGuard.validate_extraction(json.dumps(data))
        assert result is not None
        assert any("unexpected" in e.lower() for e in errors)

    def test_plausibility_total_mismatch(self):
        data = {
            "document_type": "invoice",
            "supplier_name": "Test Corp",
            "currency": "EUR",
            "items": [
                {
                    "item_name": "Widget",
                    "quantity": 10,
                    "unit_price": 5.0,
                    "total_price": 50.0,
                }
            ],
            "subtotal": 50.0,
            "tax_amount": 9.50,
            "total_amount": 100.00,  # Wrong! Should be 59.50
            "confidence_self_assessment": 0.8,
        }
        result, errors = OutputGuard.validate_extraction(json.dumps(data))
        assert result is not None
        assert any("total mismatch" in e.lower() for e in errors)

    def test_plausibility_correct_totals(self):
        data = {
            "document_type": "invoice",
            "supplier_name": "Test Corp",
            "currency": "EUR",
            "items": [
                {
                    "item_name": "Widget",
                    "quantity": 10,
                    "unit_price": 5.0,
                    "total_price": 50.0,
                }
            ],
            "subtotal": 50.0,
            "tax_amount": 9.50,
            "total_amount": 59.50,
            "confidence_self_assessment": 0.9,
        }
        result, errors = OutputGuard.validate_extraction(json.dumps(data))
        assert result is not None
        assert not any("mismatch" in e.lower() for e in errors)

    def test_invalid_document_type(self):
        data = {
            "document_type": "secret_type",
            "supplier_name": "Test Corp",
            "currency": "EUR",
            "items": [
                {
                    "item_name": "Widget",
                    "quantity": 1,
                    "unit_price": 10.0,
                    "total_price": 10.0,
                }
            ],
            "confidence_self_assessment": 0.5,
        }
        result, errors = OutputGuard.validate_extraction(json.dumps(data))
        assert result is not None  # Still parses, but flags error
        assert any("invalid document_type" in e.lower() for e in errors)

    def test_empty_string(self):
        result, errors = OutputGuard.validate_extraction("")
        assert result is None

    def test_confidence_bounds(self):
        # Too high
        data = {
            "document_type": "invoice",
            "supplier_name": "Test",
            "items": [{"item_name": "X", "quantity": 1, "unit_price": 1, "total_price": 1}],
            "confidence_self_assessment": 1.5,
        }
        result, errors = OutputGuard.validate_extraction(json.dumps(data))
        assert result is None  # Pydantic should reject > 1.0
