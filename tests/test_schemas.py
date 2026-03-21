"""Tests for Pydantic schemas."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from procurement_ai.llm.schemas import ExtractedDocument, LineItem


class TestLineItem:
    """Test LineItem schema."""

    def test_minimal(self):
        item = LineItem(
            item_name="Widget",
            quantity=Decimal("10"),
            unit_price=Decimal("5.00"),
            total_price=Decimal("50.00"),
        )
        assert item.item_name == "Widget"
        assert item.uom == "Stk"

    def test_full(self):
        item = LineItem(
            position=1,
            item_code="ITEM-001",
            item_name="Widget",
            description="A fine widget",
            quantity=Decimal("10"),
            uom="Nos",
            unit_price=Decimal("5.00"),
            total_price=Decimal("50.00"),
            tax_rate=Decimal("19.0"),
            discount_percent=Decimal("10.0"),
        )
        assert item.position == 1
        assert item.tax_rate == Decimal("19.0")

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            LineItem(quantity=Decimal("10"), unit_price=Decimal("5"))


class TestExtractedDocument:
    """Test ExtractedDocument schema."""

    def test_minimal(self):
        doc = ExtractedDocument(
            document_type="invoice",
            supplier_name="Test Corp",
            items=[
                LineItem(
                    item_name="Widget",
                    quantity=Decimal("1"),
                    unit_price=Decimal("10"),
                    total_price=Decimal("10"),
                )
            ],
            confidence_self_assessment=0.8,
        )
        assert doc.currency == "EUR"
        assert len(doc.items) == 1

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            ExtractedDocument(
                document_type="invoice",
                supplier_name="Test",
                items=[
                    LineItem(
                        item_name="X",
                        quantity=Decimal("1"),
                        unit_price=Decimal("1"),
                        total_price=Decimal("1"),
                    )
                ],
                confidence_self_assessment=1.5,
            )

    def test_json_serialization(self, sample_extraction):
        doc = ExtractedDocument.model_validate(sample_extraction)
        json_str = doc.model_dump_json()
        data = json.loads(json_str)
        assert data["supplier_name"] == "ACME GmbH"

    def test_json_schema_generation(self):
        schema = ExtractedDocument.model_json_schema()
        assert "properties" in schema
        assert "supplier_name" in schema["properties"]
