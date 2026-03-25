"""Tests for data sanitization helpers in retrospective.py."""

from __future__ import annotations

import copy
import sys
from unittest.mock import MagicMock

import pytest

# Provide a fake frappe module so the import works without a Frappe instance.
_new_mock = MagicMock()
_new_mock.utils.flt = lambda x, *a, **kw: float(x or 0)
_new_mock.utils.today = lambda: "2026-03-25"
_new_mock.utils.round_based_on_smallest_currency_fraction = lambda x, *a, **kw: x
frappe_mock = sys.modules.setdefault("frappe", _new_mock)
sys.modules.setdefault("frappe.utils", frappe_mock.utils)

from procurement_ai.chain_builder.retrospective import (
    _clean_code,
    _clean_currency,
    _clean_date,
    _clean_email,
    _clean_numeric,
    _clean_phone,
    _clean_tax_id,
    _clean_text,
    _is_discount_item,
    _is_shipping_item,
    _is_surcharge_item,
    _sanitize_line_item,
    sanitize_extracted_data,
)

# ============================================================
# _clean_text
# ============================================================


class TestCleanText:
    def test_normal_text(self):
        assert _clean_text("Hello World") == "Hello World"

    def test_strips_null_bytes(self):
        assert _clean_text("Hello\x00World") == "HelloWorld"

    def test_strips_control_chars(self):
        assert _clean_text("He\x01ll\x0eo") == "Hello"

    def test_collapses_whitespace(self):
        assert _clean_text("Hello   \t  World") == "Hello World"

    def test_strips_leading_trailing(self):
        assert _clean_text("  Hello  ") == "Hello"

    def test_truncates_at_max_len(self):
        assert _clean_text("A" * 300, max_len=200) == "A" * 200

    def test_non_string_returns_empty(self):
        assert _clean_text(123) == ""
        assert _clean_text(None) == ""

    def test_empty_string(self):
        assert _clean_text("") == ""


# ============================================================
# _clean_date
# ============================================================


class TestCleanDate:
    def test_valid_iso_date(self):
        assert _clean_date("2024-01-15") == "2024-01-15"

    def test_iso_datetime_truncated(self):
        assert _clean_date("2024-01-15T10:30:00") == "2024-01-15"

    def test_garbage_rejected(self):
        assert _clean_date("not-a-date") is None

    def test_german_format_rejected(self):
        """_clean_date only accepts YYYY-MM-DD; German format is handled by FieldValidator."""
        assert _clean_date("15.01.2024") is None

    def test_non_string_returns_none(self):
        assert _clean_date(20240115) is None
        assert _clean_date(None) is None

    def test_strips_whitespace(self):
        assert _clean_date("  2024-01-15  ") == "2024-01-15"


# ============================================================
# _clean_numeric
# ============================================================


class TestCleanNumeric:
    def test_float_passthrough(self):
        assert _clean_numeric(25.5) == 25.5

    def test_int_to_float(self):
        assert _clean_numeric(100) == 100.0

    def test_string_to_float(self):
        assert _clean_numeric("42.5") == 42.5

    def test_none_returns_none(self):
        assert _clean_numeric(None) is None

    def test_garbage_returns_none(self):
        assert _clean_numeric("not-a-number") is None

    def test_zero(self):
        assert _clean_numeric(0) == 0.0


# ============================================================
# _clean_tax_id
# ============================================================


class TestCleanTaxId:
    def test_german_vat(self):
        assert _clean_tax_id("DE123456789") == "DE123456789"

    def test_austrian_vat(self):
        assert _clean_tax_id("ATU12345678") == "ATU12345678"

    def test_strips_whitespace(self):
        assert _clean_tax_id("DE 123 456 789") == "DE123456789"

    def test_numeric_only_id(self):
        assert _clean_tax_id("12345678") == "12345678"

    def test_invalid_format_rejected(self):
        assert _clean_tax_id("INVALID!@#") == ""

    def test_non_string_returns_empty(self):
        assert _clean_tax_id(123) == ""

    def test_empty_string(self):
        assert _clean_tax_id("") == ""


# ============================================================
# _clean_email
# ============================================================


class TestCleanEmail:
    def test_valid_email(self):
        assert _clean_email("info@acme.de") == "info@acme.de"

    def test_uppercased_lowered(self):
        assert _clean_email("INFO@ACME.DE") == "info@acme.de"

    def test_invalid_email_rejected(self):
        assert _clean_email("not-an-email") == ""

    def test_non_string_returns_empty(self):
        assert _clean_email(None) == ""

    def test_empty_string(self):
        assert _clean_email("") == ""


# ============================================================
# _clean_phone
# ============================================================


class TestCleanPhone:
    def test_normal_phone(self):
        assert _clean_phone("+49 30 1234567") == "+49 30 1234567"

    def test_slash_replaced_with_space(self):
        """Frappe rejects '/' in phone fields — CLAUDE.md requirement."""
        assert _clean_phone("030/1234567") == "030 1234567"

    def test_multiple_slashes(self):
        assert _clean_phone("030/123/456") == "030 123 456"

    def test_special_chars_stripped(self):
        assert _clean_phone("+49 (30) 1234-567") == "+49 (30) 1234-567"

    def test_non_string_returns_empty(self):
        assert _clean_phone(None) == ""

    def test_truncated_at_30(self):
        assert len(_clean_phone("+" + "1" * 50)) == 30


# ============================================================
# _clean_code
# ============================================================


class TestCleanCode:
    def test_normal_code(self):
        assert _clean_code("ITEM-001") == "ITEM-001"

    def test_dots_allowed(self):
        assert _clean_code("M8x50.A2") == "M8x50.A2"

    def test_special_chars_stripped(self):
        assert _clean_code("ITEM!@#$%001") == "ITEM001"

    def test_non_string_returns_empty(self):
        assert _clean_code(None) == ""

    def test_truncated_at_140(self):
        assert len(_clean_code("A" * 200)) == 140


# ============================================================
# _clean_currency
# ============================================================


class TestCleanCurrency:
    def test_eur(self):
        assert _clean_currency("EUR") == "EUR"

    def test_lowercase_uppercased(self):
        assert _clean_currency("usd") == "USD"

    def test_invalid_returns_eur(self):
        assert _clean_currency("EURO") == "EUR"

    def test_non_string_returns_eur(self):
        assert _clean_currency(None) == "EUR"

    def test_empty_returns_eur(self):
        assert _clean_currency("") == "EUR"


# ============================================================
# _is_shipping_item / _is_discount_item / _is_surcharge_item
# ============================================================


class TestItemClassification:
    # Shipping
    def test_shipping_by_keyword_versand(self):
        assert _is_shipping_item({"item_name": "Versandkosten"}) is True

    def test_shipping_by_carrier_dhl(self):
        assert _is_shipping_item({"item_name": "DHL Paket Deutschland"}) is True

    def test_shipping_by_keyword_freight(self):
        assert _is_shipping_item({"item_name": "Freight charges"}) is True

    def test_normal_item_not_shipping(self):
        assert _is_shipping_item({"item_name": "Schrauben M8x50"}) is False

    def test_shipping_none_name(self):
        assert _is_shipping_item({"item_name": None}) is False

    # Discount — requires keyword AND negative total_price
    def test_discount_rabatt_negative(self):
        assert _is_discount_item({"item_name": "Rabatt 5%", "total_price": -10.0}) is True

    def test_discount_skonto_negative(self):
        assert _is_discount_item({"item_name": "Skonto 2%", "total_price": -5.0}) is True

    def test_discount_keyword_but_positive_price(self):
        """Discount requires negative total_price."""
        assert _is_discount_item({"item_name": "Rabatt", "total_price": 10.0}) is False

    def test_discount_negative_but_no_keyword(self):
        """Negative price alone is not a discount — needs keyword."""
        assert _is_discount_item({"item_name": "Schrauben", "total_price": -10.0}) is False

    # Surcharge
    def test_surcharge_mindermengenaufschlag(self):
        assert _is_surcharge_item({"item_name": "Mindermengenaufschlag"}) is True

    def test_surcharge_small_order(self):
        assert _is_surcharge_item({"item_name": "Small order surcharge"}) is True

    def test_normal_item_not_surcharge(self):
        assert _is_surcharge_item({"item_name": "Muttern M8"}) is False


# ============================================================
# _sanitize_line_item
# ============================================================


class TestSanitizeLineItem:
    def test_normal_item(self):
        result = _sanitize_line_item({
            "position": 1,
            "item_name": "Schrauben M8x50",
            "quantity": 100,
            "unit_price": 0.15,
            "total_price": 15.00,
        })
        assert result["item_name"] == "Schrauben M8x50"
        assert result["quantity"] == 100.0
        assert result["unit_price"] == 0.15
        assert result["total_price"] == 15.00

    def test_missing_quantity_defaults_to_1(self):
        result = _sanitize_line_item({"item_name": "Widget"})
        assert result["quantity"] == 1

    def test_missing_item_name_defaults(self):
        result = _sanitize_line_item({})
        assert result["item_name"] == "Unknown Item"

    def test_item_type_stock(self):
        result = _sanitize_line_item({"item_name": "Widget", "item_type": "stock"})
        assert result["item_type"] == "stock"

    def test_item_type_service(self):
        result = _sanitize_line_item({"item_name": "Setup", "item_type": "service"})
        assert result["item_type"] == "service"

    def test_item_type_invalid_becomes_none(self):
        result = _sanitize_line_item({"item_name": "Widget", "item_type": "unknown"})
        assert result["item_type"] is None

    def test_non_dict_returns_empty(self):
        assert _sanitize_line_item("not a dict") == {}

    def test_control_chars_in_item_name(self):
        result = _sanitize_line_item({"item_name": "Widget\x00\x01"})
        assert result["item_name"] == "Widget"


# ============================================================
# sanitize_extracted_data (integration)
# ============================================================

SAMPLE_DATA = {
    "document_type": "invoice",
    "supplier_name": "ACME GmbH",
    "supplier_address": "Musterstr. 1, 12345 Berlin",
    "supplier_tax_id": "DE123456789",
    "supplier_email": "info@acme.de",
    "supplier_phone": "030/1234567",
    "document_number": "RE-2024-001",
    "document_date": "2024-01-15",
    "delivery_date": "2024-01-20",
    "payment_terms": "30 Tage netto",
    "currency": "EUR",
    "subtotal": 25.00,
    "tax_amount": 4.75,
    "total_amount": 29.75,
    "shipping_cost": 0.0,
    "items": [
        {
            "position": 1,
            "item_name": "Schrauben M8x50",
            "quantity": 100,
            "unit_price": 0.15,
            "total_price": 15.00,
        },
        {
            "position": 2,
            "item_name": "Muttern M8",
            "quantity": 100,
            "unit_price": 0.10,
            "total_price": 10.00,
        },
    ],
}


class TestSanitizeExtractedData:
    def test_basic_passthrough(self):
        result = sanitize_extracted_data(copy.deepcopy(SAMPLE_DATA))
        assert result["supplier_name"] == "ACME GmbH"
        assert result["document_type"] == "invoice"
        assert result["currency"] == "EUR"
        assert len(result["items"]) == 2

    def test_phone_slash_cleaned(self):
        result = sanitize_extracted_data(copy.deepcopy(SAMPLE_DATA))
        assert "/" not in result["supplier_phone"]

    def test_invalid_document_type_defaults_to_invoice(self):
        data = copy.deepcopy(SAMPLE_DATA)
        data["document_type"] = "garbage_type"
        result = sanitize_extracted_data(data)
        assert result["document_type"] == "invoice"

    def test_valid_document_types(self):
        for dt in ("cart", "order_confirmation", "delivery_note", "invoice"):
            data = copy.deepcopy(SAMPLE_DATA)
            data["document_type"] = dt
            result = sanitize_extracted_data(data)
            assert result["document_type"] == dt

    def test_shipping_item_removed_when_shipping_cost_set(self):
        data = copy.deepcopy(SAMPLE_DATA)
        data["shipping_cost"] = 5.99
        data["items"].append({
            "item_name": "Versandkosten",
            "quantity": 1,
            "unit_price": 5.99,
            "total_price": 5.99,
        })
        result = sanitize_extracted_data(data)
        names = [i["item_name"] for i in result["items"]]
        assert "Versandkosten" not in names
        assert len(result["items"]) == 2

    def test_shipping_item_kept_when_no_shipping_cost(self):
        data = copy.deepcopy(SAMPLE_DATA)
        data["shipping_cost"] = 0.0
        data["items"].append({
            "item_name": "Versandkosten",
            "quantity": 1,
            "unit_price": 5.99,
            "total_price": 5.99,
        })
        result = sanitize_extracted_data(data)
        names = [i["item_name"] for i in result["items"]]
        assert "Versandkosten" in names

    def test_discount_items_extracted(self):
        data = copy.deepcopy(SAMPLE_DATA)
        data["items"].append({
            "item_name": "Rabatt 5%",
            "quantity": 1,
            "unit_price": -2.50,
            "total_price": -2.50,
        })
        result = sanitize_extracted_data(data)
        assert result["discount_amount"] == 2.50
        names = [i["item_name"] for i in result["items"]]
        assert "Rabatt 5%" not in names

    def test_multiple_discounts_summed(self):
        data = copy.deepcopy(SAMPLE_DATA)
        data["items"].extend([
            {"item_name": "Rabatt", "quantity": 1, "unit_price": -1.0, "total_price": -1.0},
            {"item_name": "Skonto 2%", "quantity": 1, "unit_price": -0.50, "total_price": -0.50},
        ])
        result = sanitize_extracted_data(data)
        assert result["discount_amount"] == pytest.approx(1.50)

    def test_no_discount_items_sets_none(self):
        result = sanitize_extracted_data(copy.deepcopy(SAMPLE_DATA))
        assert result["discount_amount"] is None

    def test_surcharge_items_extracted(self):
        data = copy.deepcopy(SAMPLE_DATA)
        data["items"].append({
            "item_name": "Mindermengenaufschlag",
            "quantity": 1,
            "unit_price": 8.50,
            "total_price": 8.50,
        })
        result = sanitize_extracted_data(data)
        assert result["surcharge_amount"] == 8.50
        names = [i["item_name"] for i in result["items"]]
        assert "Mindermengenaufschlag" not in names

    def test_mixed_special_items(self):
        """Shipping + discount + surcharge all extracted correctly."""
        data = copy.deepcopy(SAMPLE_DATA)
        data["shipping_cost"] = 5.99
        data["items"].extend([
            {"item_name": "DHL Versand", "quantity": 1, "unit_price": 5.99, "total_price": 5.99},
            {"item_name": "Rabatt", "quantity": 1, "unit_price": -2.0, "total_price": -2.0},
            {"item_name": "Kleinmengenzuschlag", "quantity": 1, "unit_price": 3.0, "total_price": 3.0},
        ])
        result = sanitize_extracted_data(data)
        assert len(result["items"]) == 2  # only original items remain
        assert result["discount_amount"] == 2.0
        assert result["surcharge_amount"] == 3.0

    def test_unknown_keys_dropped(self):
        data = copy.deepcopy(SAMPLE_DATA)
        data["malicious_key"] = "should be dropped"
        result = sanitize_extracted_data(data)
        assert "malicious_key" not in result

    def test_non_list_items_becomes_empty(self):
        data = copy.deepcopy(SAMPLE_DATA)
        data["items"] = "not a list"
        result = sanitize_extracted_data(data)
        assert result["items"] == []

    def test_empty_data(self):
        result = sanitize_extracted_data({})
        assert result["supplier_name"] == ""
        assert result["currency"] == "EUR"
        assert result["document_type"] == "invoice"
        assert result["items"] == []
