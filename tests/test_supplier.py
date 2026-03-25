"""Tests for supplier management in supplier.py and supplier_matcher.py."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Provide a fake frappe module so the import works without a Frappe instance.
_new_mock = MagicMock()
_new_mock.utils.flt = lambda x, *a, **kw: float(x or 0)
_new_mock.utils.today = lambda: "2026-03-25"
_new_mock.utils.round_based_on_smallest_currency_fraction = lambda x, *a, **kw: round(x, 2)
frappe_mock = sys.modules.setdefault("frappe", _new_mock)
sys.modules.setdefault("frappe.utils", frappe_mock.utils)

from procurement_ai.chain_builder.supplier import (
    _create_supplier,
    _detect_country,
    _get_default_supplier_group,
    _parse_address,
    ensure_supplier,
)
from procurement_ai.validation.supplier_matcher import (
    SupplierMatch,
    SupplierMatcher,
    _extract_name_keywords,
    _normalize_phone,
)


def _reset():
    frappe_mock.reset_mock()
    frappe_mock.db.get_value.side_effect = None
    frappe_mock.db.get_value.return_value = None
    frappe_mock.db.get_single_value.side_effect = None
    frappe_mock.db.get_single_value.return_value = None
    frappe_mock.get_all.side_effect = None
    frappe_mock.get_all.return_value = []
    frappe_mock.db.sql.side_effect = None
    frappe_mock.db.sql.return_value = []


# ============================================================
# _detect_country
# ============================================================


class TestDetectCountry:
    def test_german_tax_id(self):
        assert _detect_country({"supplier_tax_id": "DE123456789"}) == "Germany"

    def test_austrian_tax_id(self):
        assert _detect_country({"supplier_tax_id": "ATU12345678"}) == "Austria"

    def test_swiss_tax_id(self):
        assert _detect_country({"supplier_tax_id": "CHE123456789"}) == "Switzerland"

    def test_german_address_keyword(self):
        assert _detect_country({"supplier_address": "Musterstr. 1, Deutschland"}) == "Germany"

    def test_austria_address_keyword(self):
        assert _detect_country({"supplier_address": "Wien, Österreich"}) == "Austria"

    def test_default_is_germany(self):
        assert _detect_country({}) == "Germany"


# ============================================================
# _parse_address
# ============================================================


class TestParseAddress:
    def test_german_address(self):
        line1, city, pincode = _parse_address("Musterstr. 1, 12345 Berlin")
        assert line1 == "Musterstr. 1"
        assert city == "Berlin"
        assert pincode == "12345"

    def test_multiline_address(self):
        line1, city, pincode = _parse_address("Musterstr. 1\n12345 Berlin")
        assert line1 == "Musterstr. 1"
        assert city == "Berlin"
        assert pincode == "12345"

    def test_no_pincode_uses_last_line(self):
        line1, city, pincode = _parse_address("Musterstr. 1, Berlin")
        assert line1 == "Musterstr. 1"
        assert city == "Berlin"
        assert pincode == ""

    def test_empty_address(self):
        assert _parse_address("") == ("", "", "")

    def test_single_line(self):
        line1, city, pincode = _parse_address("Berlin")
        assert line1 == "Berlin"
        assert city == ""
        assert pincode == ""


# ============================================================
# _get_default_supplier_group
# ============================================================


class TestGetDefaultSupplierGroup:
    def setup_method(self):
        _reset()

    def test_uses_buying_settings(self):
        frappe_mock.db.get_single_value.return_value = "Default Supplier Group"
        assert _get_default_supplier_group() == "Default Supplier Group"

    def test_fallback_to_first_non_group(self):
        frappe_mock.db.get_single_value.return_value = None
        frappe_mock.get_all.side_effect = [
            [{"name": "Raw Materials"}],  # first non-group
        ]
        assert _get_default_supplier_group() == "Raw Materials"

    def test_fallback_to_any_group(self):
        frappe_mock.db.get_single_value.return_value = None
        frappe_mock.get_all.side_effect = [
            [],  # no non-group
            [{"name": "All Supplier Groups"}],  # any group
        ]
        assert _get_default_supplier_group() == "All Supplier Groups"


# ============================================================
# _create_supplier
# ============================================================


class TestCreateSupplier:
    def setup_method(self):
        _reset()
        frappe_mock.db.get_single_value.return_value = "Default Group"

    def test_creates_with_name(self):
        mock_doc = MagicMock()
        mock_doc.name = "ACME GmbH"
        frappe_mock.get_doc.return_value = mock_doc

        result = _create_supplier({"supplier_name": "ACME GmbH", "supplier_tax_id": "DE123456789"})
        assert result == "ACME GmbH"
        mock_doc.insert.assert_called_once_with(ignore_permissions=True)

    def test_sets_tax_id(self):
        mock_doc = MagicMock()
        mock_doc.name = "ACME"
        frappe_mock.get_doc.return_value = mock_doc

        _create_supplier({"supplier_name": "ACME", "supplier_tax_id": "DE123"})
        assert mock_doc.tax_id == "DE123"

    def test_creates_address_when_present(self):
        mock_doc = MagicMock()
        mock_doc.name = "ACME"
        frappe_mock.get_doc.return_value = mock_doc

        _create_supplier({
            "supplier_name": "ACME",
            "supplier_address": "Musterstr. 1, 12345 Berlin",
        })
        # get_doc called for Supplier + Address = at least 2 times
        assert frappe_mock.get_doc.call_count >= 2


# ============================================================
# ensure_supplier (integration)
# ============================================================


class TestEnsureSupplier:
    def setup_method(self):
        _reset()

    @patch.object(SupplierMatcher, "find_match")
    def test_returns_existing_match(self, mock_find):
        mock_find.return_value = SupplierMatch(
            found=True, supplier_name="Existing GmbH",
            match_confidence=1.0, match_method="tax_id",
        )
        result = ensure_supplier({"supplier_name": "Existing GmbH"})
        assert result == "Existing GmbH"

    @patch.object(SupplierMatcher, "find_match")
    def test_creates_new_when_no_match(self, mock_find):
        mock_find.return_value = SupplierMatch(found=False)
        frappe_mock.db.get_single_value.return_value = "Default Group"
        mock_doc = MagicMock()
        mock_doc.name = "New Supplier"
        frappe_mock.get_doc.return_value = mock_doc

        result = ensure_supplier({"supplier_name": "New Supplier"})
        assert result == "New Supplier"
        mock_doc.insert.assert_called()


# ============================================================
# supplier_matcher helpers
# ============================================================


class TestNormalizePhone:
    def test_german_number_normalized(self):
        """German country code +49 is normalized to domestic 0-prefix."""
        result = _normalize_phone("+49 (30) 1234567")
        assert result == "0301234567"

    def test_domestic_number_unchanged(self):
        result = _normalize_phone("030 1234567")
        assert result == "0301234567"

    def test_empty_returns_empty(self):
        assert _normalize_phone("") == ""


class TestExtractNameKeywords:
    def test_filters_legal_forms(self):
        kws = _extract_name_keywords("ACME GmbH")
        assert "acme" in kws
        assert "gmbh" not in kws

    def test_filters_short_words(self):
        kws = _extract_name_keywords("A B ACME")
        assert "acme" in kws
        assert "a" not in kws

    def test_empty_name(self):
        assert _extract_name_keywords("") == []


# ============================================================
# SupplierMatcher.find_match
# ============================================================


class TestSupplierMatcherFindMatch:
    def setup_method(self):
        _reset()

    def test_match_by_tax_id(self):
        # _match_by_tax_id uses frappe.get_all, not db.get_value
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = [{"name": "Existing GmbH"}]
        result = SupplierMatcher.find_match({
            "supplier_name": "ACME",
            "supplier_tax_id": "DE123456789",
        })
        assert result.found is True
        assert result.match_method == "tax_id"

    def test_no_match_returns_not_found(self):
        frappe_mock.db.get_value.return_value = None
        frappe_mock.db.sql.return_value = []
        result = SupplierMatcher.find_match({
            "supplier_name": "Unknown Corp",
        })
        assert result.found is False
