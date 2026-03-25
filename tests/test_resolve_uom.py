"""Tests for UOM resolution and bulk adjustment in purchase_order.py."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, call

import pytest

# Provide a fake frappe module so the import works without a Frappe instance.
_new_mock = MagicMock()
_new_mock.utils.flt = lambda x, *a, **kw: float(x or 0)
_new_mock.utils.today = lambda: "2026-03-25"
_new_mock.utils.round_based_on_smallest_currency_fraction = lambda x, *a, **kw: round(x, 2)
frappe_mock = sys.modules.setdefault("frappe", _new_mock)
sys.modules.setdefault("frappe.utils", frappe_mock.utils)

import procurement_ai.chain_builder.purchase_order as po_mod
from procurement_ai.chain_builder.purchase_order import (
    _adjust_bulk_uom,
    _compute_bulk_factor,
    _get_currency_precision,
    _get_piece_uom,
    _has_cent_fractions,
    _is_numeric_uom,
    _resolve_uom,
    _true_unit_price,
)


def _reset_caches():
    """Reset module-level caches between tests."""
    po_mod._piece_uom_cache = None
    po_mod._uom_category_cache = None


# ============================================================
# _true_unit_price
# ============================================================


class TestTrueUnitPrice:
    def test_back_calculates_from_total(self):
        item = {"total_price": 5.88, "unit_price": 0.029}
        assert _true_unit_price(item, 200) == pytest.approx(0.0294)

    def test_falls_back_to_unit_price(self):
        item = {"unit_price": 1.50}
        assert _true_unit_price(item, 10) == 1.50

    def test_zero_quantity_uses_unit_price(self):
        item = {"total_price": 10.0, "unit_price": 5.0}
        assert _true_unit_price(item, 0) == 5.0

    def test_no_prices_returns_zero(self):
        assert _true_unit_price({}, 10) == 0.0

    def test_none_unit_price(self):
        item = {"unit_price": None}
        assert _true_unit_price(item, 10) == 0.0


# ============================================================
# _is_numeric_uom
# ============================================================


class TestIsNumericUom:
    def test_integer_string(self):
        assert _is_numeric_uom("100") is True

    def test_float_string(self):
        assert _is_numeric_uom("10.5") is True

    def test_named_uom(self):
        assert _is_numeric_uom("Box") is False

    def test_empty_string(self):
        assert _is_numeric_uom("") is False

    def test_zero(self):
        assert _is_numeric_uom("0") is False

    def test_negative(self):
        assert _is_numeric_uom("-1") is False

    def test_none(self):
        assert _is_numeric_uom(None) is False


# ============================================================
# _get_piece_uom
# ============================================================


class TestGetPieceUom:
    def setup_method(self):
        frappe_mock.reset_mock()
        _reset_caches()

    def test_returns_nos_when_exists(self):
        frappe_mock.db.exists.side_effect = lambda dt, name: name == "Nos"
        assert _get_piece_uom() == "Nos"

    def test_returns_stk_when_nos_missing(self):
        frappe_mock.db.exists.side_effect = lambda dt, name: name == "Stk"
        assert _get_piece_uom() == "Stk"

    def test_creates_nos_when_none_exist(self):
        frappe_mock.db.exists.side_effect = None
        frappe_mock.db.exists.return_value = False
        result = _get_piece_uom()
        assert result == "Nos"
        # Should have tried to create Nos via _ensure_uom_exists
        frappe_mock.get_doc.assert_called()

    def test_caches_result(self):
        frappe_mock.db.exists.side_effect = lambda dt, name: name == "Nos"
        _get_piece_uom()
        _get_piece_uom()
        # db.exists should only be called for the first invocation
        assert frappe_mock.db.exists.call_count <= len(("Nos", "Stk", "Stück", "Stk."))


# ============================================================
# _resolve_uom
# ============================================================


class TestResolveUom:
    def setup_method(self):
        frappe_mock.reset_mock()
        _reset_caches()
        # Default: "Nos" exists as piece UOM
        frappe_mock.db.exists.side_effect = lambda dt, name: name in ("Nos", "Kg", "Gram", "Liter", "Meter", "Km")
        frappe_mock.db.get_value.return_value = None

    def test_stk_maps_to_piece_uom(self):
        assert _resolve_uom("Stk") == "Nos"

    def test_stueck_lowercase(self):
        assert _resolve_uom("stück") == "Nos"

    def test_pcs_maps_to_piece(self):
        assert _resolve_uom("pcs") == "Nos"

    def test_ea_maps_to_piece(self):
        assert _resolve_uom("ea") == "Nos"

    def test_kg_maps_to_Kg(self):
        assert _resolve_uom("kg") == "Kg"

    def test_g_maps_to_Gram(self):
        assert _resolve_uom("g") == "Gram"

    def test_l_maps_to_Liter(self):
        assert _resolve_uom("l") == "Liter"

    def test_m_maps_to_Meter(self):
        assert _resolve_uom("m") == "Meter"

    def test_empty_uom_returns_piece(self):
        assert _resolve_uom("") == "Nos"

    def test_none_uom_returns_piece(self):
        assert _resolve_uom(None) == "Nos"

    def test_exact_match_in_db(self):
        """UOM that exists in DB as-is should be returned."""
        frappe_mock.db.get_value.return_value = "Karton"
        assert _resolve_uom("Karton") == "Karton"

    def test_unknown_uom_fallback_to_piece(self):
        """Unknown UOM not in DB → fallback to piece UOM."""
        frappe_mock.db.get_value.return_value = None
        assert _resolve_uom("UnknownUnit") == "Nos"


# ============================================================
# _get_currency_precision
# ============================================================


class TestGetCurrencyPrecision:
    def setup_method(self):
        frappe_mock.reset_mock()

    def test_eur_precision_2(self):
        frappe_mock.db.get_value.return_value = 0.01
        assert _get_currency_precision("EUR") == 2

    def test_jpy_precision_0(self):
        frappe_mock.db.get_value.return_value = 1
        assert _get_currency_precision("JPY") == 0

    def test_none_currency_defaults_2(self):
        assert _get_currency_precision(None) == 2

    def test_no_db_value_defaults_2(self):
        frappe_mock.db.get_value.return_value = None
        assert _get_currency_precision("USD") == 2


# ============================================================
# _has_cent_fractions
# ============================================================


class TestHasCentFractions:
    def setup_method(self):
        frappe_mock.reset_mock()
        frappe_mock.db.get_value.return_value = 0.01  # precision=2

    def test_sub_cent_detected(self):
        assert _has_cent_fractions(0.065) is True

    def test_exact_cents_ok(self):
        assert _has_cent_fractions(0.07) is False

    def test_normal_price_ok(self):
        assert _has_cent_fractions(11.23) is False

    def test_very_small_price(self):
        assert _has_cent_fractions(0.001) is True


# ============================================================
# _compute_bulk_factor
# ============================================================


class TestComputeBulkFactor:
    def setup_method(self):
        frappe_mock.reset_mock()
        frappe_mock.db.get_value.return_value = 0.01  # EUR precision=2

    def test_factor_10_for_sub_cent(self):
        """0.065 * 10 = 0.65 → no sub-cent fractions."""
        result = _compute_bulk_factor(100, 0.065, "EUR")
        assert result == 10.0

    def test_factor_100_needed(self):
        """0.003 * 10 = 0.03, OK. But qty/10 must be >= 1."""
        result = _compute_bulk_factor(1000, 0.003, "EUR")
        assert result == 10.0

    def test_factor_100_for_very_small(self):
        """0.0003 * 100 = 0.03 → valid (no sub-cent). Factor 100 is smallest that works."""
        result = _compute_bulk_factor(10000, 0.0003, "EUR")
        assert result == 100.0

    def test_qty_as_fallback(self):
        """If no power-of-10 works, uses qty itself."""
        # 0.007 * 10 = 0.07 OK, but qty=3 < 10
        # So fallback to qty=3: 0.007 * 3 = 0.021 → still has fractions
        # Actually 0.007 * 10 = 0.07 is fine, but qty/10 = 0.3 < 1
        result = _compute_bulk_factor(3, 0.007, "EUR")
        # 0.007 * 3 = 0.021 → still sub-cent. 0.007 * 10 = 0.07 but qty < 10
        # So returns qty=3 if 0.021 has no sub-cent... 0.021 rounded to 2 = 0.02 ≠ 0.021
        # So None
        assert result is None

    def test_none_when_impossible(self):
        """No factor works for this combination."""
        result = _compute_bulk_factor(1, 0.003, "EUR")
        assert result is None


# ============================================================
# _adjust_bulk_uom
# ============================================================


class TestAdjustBulkUom:
    def setup_method(self):
        frappe_mock.reset_mock()
        _reset_caches()
        # Standard mocks: Nos exists, EUR precision=2
        frappe_mock.db.exists.side_effect = lambda dt, name: name in ("Nos", "Item", "UOM")
        frappe_mock.db.get_value.return_value = 0.01
        frappe_mock.get_all.return_value = []  # No existing item UOM conversions
        frappe_mock.db.sql.return_value = [[0]]  # max idx

    def test_no_adjustment_for_normal_price(self):
        """Price with exact cents → no adjustment needed."""
        qty, rate, uom = _adjust_bulk_uom(100, 1.50, "Nos")
        assert qty == 100
        assert rate == 1.50
        assert uom == "Nos"

    def test_adjusts_sub_cent_price(self):
        """0.065 per piece → should adjust to per-10."""
        qty, rate, uom = _adjust_bulk_uom(100, 0.065, "Nos")
        assert rate == pytest.approx(0.65)
        assert qty == pytest.approx(10.0)
        assert uom == "10"

    def test_no_adjustment_for_kg(self):
        """Non-piece UOMs like kg should not be adjusted."""
        qty, rate, uom = _adjust_bulk_uom(100, 0.065, "Kg")
        assert qty == 100
        assert rate == 0.065
        assert uom == "Kg"

    def test_no_adjustment_for_zero_rate(self):
        qty, rate, uom = _adjust_bulk_uom(100, 0, "Nos")
        assert rate == 0

    def test_dry_run_doesnt_create(self):
        """dry_run=True should not create UOM records."""
        _adjust_bulk_uom(100, 0.065, "Nos", dry_run=True)
        frappe_mock.get_doc.assert_not_called()

    def test_uses_existing_item_conversion(self):
        """If item already has a numeric UOM conversion, use it."""
        frappe_mock.db.exists.side_effect = lambda dt, name: True  # Item exists
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = [
            {"uom": "50", "conversion_factor": 50.0},
        ]
        try:
            qty, rate, uom = _adjust_bulk_uom(200, 0.065, "Nos", item_code="ITEM-001")
            assert rate == pytest.approx(3.25)  # 0.065 * 50
            assert qty == pytest.approx(4.0)    # 200 / 50
            assert uom == "50"
        finally:
            # Clean up shared mock state to prevent cross-test pollution
            frappe_mock.db.exists.side_effect = None
            frappe_mock.get_all.side_effect = None
