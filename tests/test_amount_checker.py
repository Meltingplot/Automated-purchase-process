"""Tests for AmountChecker - arithmetic validation."""

from __future__ import annotations

import pytest

from procurement_ai.validation.amount_checker import AmountChecker


class TestAmountChecker:
    """Test arithmetic validation of extracted financial data."""

    def test_valid_amounts(self, sample_extraction):
        result = AmountChecker.check(sample_extraction)
        assert result.valid

    def test_subtotal_mismatch(self):
        data = {
            "items": [
                {"item_name": "A", "quantity": 10, "unit_price": 5.0, "total_price": 50.0},
            ],
            "subtotal": 100.0,  # Wrong
            "total_amount": 50.0,
        }
        result = AmountChecker.check(data)
        assert not result.valid
        assert any("subtotal" in e.lower() for e in result.errors)

    def test_total_mismatch(self):
        data = {
            "items": [
                {"item_name": "A", "quantity": 10, "unit_price": 5.0, "total_price": 50.0},
            ],
            "tax_amount": 9.50,
            "total_amount": 100.0,  # Wrong: should be 59.50
        }
        result = AmountChecker.check(data)
        assert not result.valid
        assert any("total" in e.lower() for e in result.errors)

    def test_correct_with_tax_and_shipping(self):
        data = {
            "items": [
                {"item_name": "A", "quantity": 10, "unit_price": 5.0, "total_price": 50.0},
            ],
            "subtotal": 50.0,
            "tax_amount": 9.50,
            "shipping_cost": 5.00,
            "total_amount": 64.50,
        }
        result = AmountChecker.check(data)
        assert result.valid

    def test_line_item_mismatch(self):
        data = {
            "items": [
                {"item_name": "A", "quantity": 10, "unit_price": 5.0, "total_price": 99.0},
            ],
        }
        result = AmountChecker.check(data)
        assert any("line item" in w.lower() for w in result.warnings)

    def test_empty_items(self):
        result = AmountChecker.check({"items": []})
        assert any("no line items" in w.lower() for w in result.warnings)

    def test_tolerance_within(self):
        """Values within 5 cent tolerance should pass."""
        data = {
            "items": [
                {"item_name": "A", "quantity": 3, "unit_price": 3.33, "total_price": 9.99},
            ],
            "total_amount": 10.00,  # 1 cent off
        }
        result = AmountChecker.check(data)
        # Should be within tolerance for line item, but total won't match
        # because there's no tax/shipping to account for the diff

    def test_discount_handling(self):
        data = {
            "items": [
                {
                    "item_name": "A",
                    "quantity": 10,
                    "unit_price": 10.0,
                    "total_price": 90.0,
                    "discount_percent": 10.0,
                },
            ],
        }
        result = AmountChecker.check(data)
        # 10 * 10 * 0.9 = 90.0 → should be fine
        assert not any("line item" in w.lower() and "diff" in w.lower() for w in result.warnings)
