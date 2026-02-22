"""Tests for the extraction pipeline and Pydantic schemas."""

import json
from datetime import date

import pytest

from purchase_automation.extraction.schemas import (
    DocumentType,
    ExtractedDocument,
    ExtractedLineItem,
)


class TestExtractedDocument:
    def test_valid_invoice(self, sample_invoice_data):
        doc = ExtractedDocument.model_validate(sample_invoice_data)
        assert doc.document_type == DocumentType.PURCHASE_INVOICE
        assert doc.supplier_name == "Muster GmbH"
        assert len(doc.line_items) == 2
        assert doc.total_amount == 595.00

    def test_valid_shopping_cart(self):
        data = {
            "document_type": "shopping_cart",
            "supplier_name": "Online Shop XY",
            "line_items": [
                {
                    "item_description": "Test Product",
                    "quantity": 1,
                    "unit_price": 10.00,
                }
            ],
            "currency": "EUR",
        }
        doc = ExtractedDocument.model_validate(data)
        assert doc.document_type == DocumentType.SHOPPING_CART

    def test_rejects_empty_supplier(self):
        data = {
            "document_type": "purchase_invoice",
            "supplier_name": "",
            "line_items": [{"item_description": "X", "quantity": 1}],
        }
        with pytest.raises(Exception):
            ExtractedDocument.model_validate(data)

    def test_rejects_no_line_items(self):
        data = {
            "document_type": "purchase_invoice",
            "supplier_name": "Test",
            "line_items": [],
        }
        with pytest.raises(Exception):
            ExtractedDocument.model_validate(data)

    def test_rejects_negative_quantity(self):
        data = {
            "document_type": "purchase_invoice",
            "supplier_name": "Test",
            "line_items": [{"item_description": "X", "quantity": -5}],
        }
        with pytest.raises(Exception):
            ExtractedDocument.model_validate(data)

    def test_rejects_invalid_currency(self):
        data = {
            "document_type": "purchase_invoice",
            "supplier_name": "Test",
            "line_items": [{"item_description": "X", "quantity": 1}],
            "currency": "euro",  # must be uppercase 3-letter code
        }
        with pytest.raises(Exception):
            ExtractedDocument.model_validate(data)

    def test_plausibility_warning_on_inconsistent_totals(self):
        data = {
            "document_type": "purchase_invoice",
            "supplier_name": "Test",
            "line_items": [
                {"item_description": "X", "quantity": 1, "unit_price": 100}
            ],
            "subtotal": 100.00,
            "tax_amount": 19.00,
            "total_amount": 200.00,  # Wrong! Should be 119.00
            "currency": "EUR",
        }
        doc = ExtractedDocument.model_validate(data)
        assert "PLAUSIBILITY WARNING" in (doc.notes or "")

    def test_sanitizes_control_characters(self):
        data = {
            "document_type": "purchase_invoice",
            "supplier_name": "Test",
            "line_items": [
                {
                    "item_description": "Widget\x00with\x01control\x02chars",
                    "quantity": 1,
                }
            ],
        }
        doc = ExtractedDocument.model_validate(data)
        assert "\x00" not in doc.line_items[0].item_description
        assert "\x01" not in doc.line_items[0].item_description

    def test_json_schema_generation(self):
        schema = ExtractedDocument.json_schema_for_llm()
        assert "properties" in schema
        assert "document_type" in schema["properties"]
        assert "supplier_name" in schema["properties"]
        assert "line_items" in schema["properties"]

    def test_rejects_excessive_amount(self):
        data = {
            "document_type": "purchase_invoice",
            "supplier_name": "Test",
            "line_items": [
                {
                    "item_description": "X",
                    "quantity": 1,
                    "total_price": 999_999_999,  # Over 100M limit
                }
            ],
        }
        with pytest.raises(Exception):
            ExtractedDocument.model_validate(data)

    def test_optional_fields_can_be_null(self):
        data = {
            "document_type": "delivery_note",
            "supplier_name": "Lieferant ABC",
            "line_items": [
                {"item_description": "Part 123", "quantity": 5}
            ],
        }
        doc = ExtractedDocument.model_validate(data)
        assert doc.document_number is None
        assert doc.document_date is None
        assert doc.subtotal is None
        assert doc.total_amount is None
