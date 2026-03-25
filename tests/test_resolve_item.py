"""Tests for item resolution (_resolve_item, _try_resolve_item) in purchase_order.py."""

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

import procurement_ai.chain_builder.purchase_order as po_mod
from procurement_ai.chain_builder.purchase_order import (
    _create_item,
    _extract_keywords,
    _match_by_code_and_text,
    _match_by_supplier_part_no,
    _match_by_text,
    _normalize_dimensions,
    _resolve_item,
    _try_resolve_item,
)

SETTINGS = {"default_company": "Test Company"}


def _reset():
    frappe_mock.reset_mock()
    po_mod._piece_uom_cache = None
    po_mod._uom_category_cache = None
    # Default: Nos exists
    frappe_mock.db.exists.side_effect = None
    frappe_mock.db.exists.return_value = True
    frappe_mock.db.get_value.return_value = None
    frappe_mock.db.get_single_value.return_value = "Products"
    frappe_mock.get_all.return_value = []


# ============================================================
# _extract_keywords
# ============================================================


class TestExtractKeywords:
    def test_basic_extraction(self):
        kws = _extract_keywords("Schrauben M8x50", "Edelstahl A2")
        assert "schrauben" in kws
        assert "edelstahl" in kws
        assert "m8x50" in kws

    def test_short_words_filtered(self):
        kws = _extract_keywords("A B CD", "")
        # All < 3 chars
        assert kws == []

    def test_stopwords_filtered(self):
        kws = _extract_keywords("Schraube für den Motor", "")
        assert "für" not in kws
        assert "den" not in kws
        assert "schraube" in kws
        assert "motor" in kws

    def test_sorted_longest_first(self):
        kws = _extract_keywords("ABC ABCDEF ABCD", "")
        assert kws == ["abcdef", "abcd", "abc"]

    def test_deduplication(self):
        kws = _extract_keywords("bolt bolt bolt", "")
        assert kws.count("bolt") == 1


# ============================================================
# _normalize_dimensions
# ============================================================


class TestNormalizeDimensions:
    def test_collapses_space_before_mm(self):
        assert "4mm" in _normalize_dimensions("4 mm")

    def test_collapses_space_before_kg(self):
        assert "10kg" in _normalize_dimensions("10 kg")

    def test_no_change_without_space(self):
        assert _normalize_dimensions("4mm") == "4mm"


# ============================================================
# _match_by_supplier_part_no
# ============================================================


class TestMatchBySupplierPartNo:
    def setup_method(self):
        _reset()

    def test_match_with_drop_ship(self):
        frappe_mock.get_all.return_value = [{"parent": "ITEM-001"}]
        frappe_mock.db.get_value.return_value = 1  # delivered_by_supplier=1
        result = _match_by_supplier_part_no("ACME GmbH", "ART-123")
        assert result == "ITEM-001"

    def test_match_non_drop_ship_fallback(self):
        frappe_mock.get_all.return_value = [{"parent": "ITEM-002"}]
        frappe_mock.db.get_value.return_value = 0  # delivered_by_supplier=0
        result = _match_by_supplier_part_no("ACME GmbH", "ART-123")
        assert result == "ITEM-002"

    def test_no_match(self):
        frappe_mock.get_all.return_value = []
        result = _match_by_supplier_part_no("ACME GmbH", "ART-123")
        assert result is None


# ============================================================
# _match_by_code_and_text
# ============================================================


class TestMatchByCodeAndText:
    def setup_method(self):
        _reset()

    def test_code_match_with_keyword_overlap(self):
        frappe_mock.get_all.return_value = [
            {"name": "BOLT-M8", "item_name": "Schrauben M8x50", "description": "Edelstahl"}
        ]
        result = _match_by_code_and_text("BOLT-M8", "Schrauben M8x50", "Edelstahl A2")
        assert result == "BOLT-M8"

    def test_code_match_no_keyword_overlap(self):
        frappe_mock.get_all.return_value = [
            {"name": "BOLT-M8", "item_name": "Completely Different Name", "description": ""}
        ]
        result = _match_by_code_and_text("BOLT-M8", "Schrauben M8x50", "Edelstahl A2")
        assert result is None

    def test_code_match_no_extracted_keywords(self):
        """If no keywords from extracted item, code match alone is sufficient."""
        frappe_mock.get_all.return_value = [
            {"name": "X", "item_name": "X", "description": ""}
        ]
        result = _match_by_code_and_text("X", "AB", "")  # "AB" too short for keyword
        assert result == "X"

    def test_no_candidates(self):
        frappe_mock.get_all.return_value = []
        result = _match_by_code_and_text("NOEXIST", "Widget", "")
        assert result is None


# ============================================================
# _match_by_text
# ============================================================


class TestMatchByText:
    def setup_method(self):
        _reset()

    def test_match_with_2_keywords(self):
        """Requires 2+ keyword matches to accept."""
        frappe_mock.get_all.return_value = [
            {"name": "ITEM-001", "item_name": "Schrauben M8x50 Edelstahl", "description": "A2"}
        ]
        result = _match_by_text("Schrauben M8x50", "Edelstahl A2")
        assert result == "ITEM-001"

    def test_reject_single_keyword_match(self):
        """Only 1 keyword overlap is not enough."""
        frappe_mock.get_all.return_value = [
            {"name": "ITEM-001", "item_name": "Schrauben DIN933", "description": ""}
        ]
        # "schrauben" matches, but "m8x50" and "edelstahl" don't
        result = _match_by_text("Schrauben M8x50", "Edelstahl A2")
        # Need score >= 2; "schrauben" is the only overlap in item_name
        # The search is per-keyword LIKE: first tries "edelstahl" (longest),
        # gets this match, but only 1 keyword overlaps → rejects
        assert result is None

    def test_no_keywords(self):
        result = _match_by_text("AB", "")
        assert result is None


# ============================================================
# _try_resolve_item (steps 1-3 only, no creation)
# ============================================================


class TestTryResolveItem:
    def setup_method(self):
        _reset()

    def test_returns_match_from_step1(self):
        frappe_mock.get_all.return_value = [{"parent": "ITEM-001"}]
        frappe_mock.db.get_value.return_value = 1
        item = {"item_code": "ART-123", "item_name": "Widget"}
        result = _try_resolve_item(item, SETTINGS, supplier="ACME GmbH")
        assert result == "ITEM-001"

    def test_returns_none_when_no_match(self):
        frappe_mock.get_all.return_value = []
        item = {"item_name": "XY"}
        result = _try_resolve_item(item, SETTINGS, supplier="ACME GmbH")
        assert result is None

    def test_does_not_create_item(self):
        """_try_resolve_item must NOT create items (no side effects)."""
        frappe_mock.get_all.return_value = []
        item = {"item_name": "New Widget", "item_code": "NEW-001"}
        result = _try_resolve_item(item, SETTINGS, supplier="ACME GmbH")
        assert result is None
        # Verify no Item was created
        frappe_mock.get_doc.assert_not_called()


# ============================================================
# _resolve_item (full 4-step with creation)
# ============================================================


class TestResolveItem:
    def setup_method(self):
        _reset()
        frappe_mock.generate_hash.return_value = "ABCD1234"

    def test_returns_existing_match(self):
        """If step 1-3 find a match, return it without creating."""
        frappe_mock.get_all.side_effect = [
            # _match_by_supplier_part_no query
            [{"parent": "EXISTING-ITEM"}],
            # _ensure_supplier_link query
            [{"name": "row1", "supplier_part_no": "ART-123"}],
        ]
        frappe_mock.db.get_value.return_value = 1  # delivered_by_supplier

        item = {"item_code": "ART-123", "item_name": "Widget"}
        result = _resolve_item(item, SETTINGS, supplier="ACME GmbH")
        assert result == "EXISTING-ITEM"

    def test_creates_new_item_when_no_match(self):
        """Step 4: creates new Item when steps 1-3 fail."""
        # get_all is called multiple times in _try_resolve_item: step1, step2, step3
        # All return empty → no match → falls through to _create_item
        frappe_mock.get_all.side_effect = lambda *a, **kw: []

        mock_item = MagicMock()
        mock_item.name = "SKU-ABCD1234"
        frappe_mock.get_doc.return_value = mock_item

        item = {"item_name": "Brand New Widget", "item_code": "BNW-001"}
        result = _resolve_item(item, SETTINGS, supplier="ACME GmbH")
        assert result == "SKU-ABCD1234"
        mock_item.insert.assert_called_once_with(ignore_permissions=True)


# ============================================================
# _create_item
# ============================================================


class TestCreateItem:
    def setup_method(self):
        _reset()
        frappe_mock.generate_hash.return_value = "ABCD1234"

    def _setup_mock_item(self):
        mock_item = MagicMock()
        mock_item.name = "SKU-ABCD1234"
        frappe_mock.get_doc.return_value = mock_item
        return mock_item

    def test_creates_with_correct_fields(self):
        mock_item = self._setup_mock_item()
        item = {"item_name": "Schrauben M8x50", "description": "Edelstahl A2"}
        result = _create_item(item, "ACME GmbH", SETTINGS)
        assert result == "SKU-ABCD1234"
        # Verify get_doc was called with correct doctype
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["doctype"] == "Item"
        assert call_args["item_name"] == "Schrauben M8x50"
        assert call_args["delivered_by_supplier"] == 1

    def test_service_item_type(self):
        mock_item = self._setup_mock_item()
        item = {"item_name": "Installation", "item_type": "service"}
        _create_item(item, "ACME GmbH", SETTINGS)
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["is_stock_item"] == 0

    def test_stock_item_type(self):
        mock_item = self._setup_mock_item()
        item = {"item_name": "Widget", "item_type": "stock"}
        _create_item(item, "ACME GmbH", SETTINGS)
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["is_stock_item"] == 1

    def test_supplier_link_appended(self):
        mock_item = self._setup_mock_item()
        item = {"item_name": "Widget", "item_code": "W-001"}
        _create_item(item, "ACME GmbH", SETTINGS)
        mock_item.append.assert_any_call("supplier_items", {
            "supplier": "ACME GmbH",
            "supplier_part_no": "W-001",
        })

    def test_no_supplier_link_without_supplier(self):
        mock_item = self._setup_mock_item()
        item = {"item_name": "Widget"}
        _create_item(item, "", SETTINGS)
        # supplier_items should not be appended
        for c in mock_item.append.call_args_list:
            assert c[0][0] != "supplier_items"
