"""Tests for FieldValidator - field-level validation."""

from __future__ import annotations

import pytest

from procurement_ai.validation.field_validator import FieldValidator


class TestFieldValidator:
    """Test field validation rules."""

    def test_valid_data(self, sample_extraction):
        data, warnings = FieldValidator.validate(sample_extraction)
        assert data["supplier_name"] == "ACME GmbH"
        # Valid data should have minimal warnings

    def test_german_vat_id(self):
        data = {"supplier_tax_id": "DE123456789"}
        _, warnings = FieldValidator.validate(data)
        assert not any("unrecognized tax id" in w.lower() for w in warnings)

    def test_austrian_vat_id(self):
        data = {"supplier_tax_id": "ATU12345678"}
        _, warnings = FieldValidator.validate(data)
        assert not any("unrecognized tax id" in w.lower() for w in warnings)

    def test_invalid_vat_id(self):
        data = {"supplier_tax_id": "INVALID123"}
        _, warnings = FieldValidator.validate(data)
        assert any("unrecognized tax id" in w.lower() for w in warnings)

    def test_valid_email(self):
        data = {"supplier_email": "info@example.com"}
        _, warnings = FieldValidator.validate(data)
        assert not any("email" in w.lower() for w in warnings)

    def test_invalid_email(self):
        data = {"supplier_email": "not-an-email"}
        _, warnings = FieldValidator.validate(data)
        assert any("email" in w.lower() for w in warnings)

    def test_date_iso_format(self):
        data = {"document_date": "2024-01-15"}
        data, _ = FieldValidator.validate(data)
        assert data["document_date"] == "2024-01-15"

    def test_date_german_format(self):
        data = {"document_date": "15.01.2024"}
        data, _ = FieldValidator.validate(data)
        assert data["document_date"] == "2024-01-15"

    def test_date_slash_format(self):
        data = {"document_date": "15/01/2024"}
        data, _ = FieldValidator.validate(data)
        assert data["document_date"] == "2024-01-15"

    def test_invalid_date(self):
        data = {"document_date": "not-a-date"}
        _, warnings = FieldValidator.validate(data)
        assert any("could not parse date" in w.lower() for w in warnings)

    def test_short_supplier_name(self):
        data = {"supplier_name": "X"}
        _, warnings = FieldValidator.validate(data)
        assert any("too short" in w.lower() for w in warnings)

    def test_supplier_name_trimmed(self):
        data = {"supplier_name": "  ACME GmbH  "}
        data, _ = FieldValidator.validate(data)
        assert data["supplier_name"] == "ACME GmbH"

    def test_unusual_currency(self):
        data = {"currency": "BTC"}
        _, warnings = FieldValidator.validate(data)
        assert any("unusual currency" in w.lower() for w in warnings)

    def test_valid_currencies(self):
        for currency in ["EUR", "USD", "GBP", "CHF"]:
            data = {"currency": currency}
            _, warnings = FieldValidator.validate(data)
            assert not any("currency" in w.lower() for w in warnings)
