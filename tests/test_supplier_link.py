"""Tests for _ensure_supplier_link — backfill supplier codes on matched items."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, call, patch

import pytest

# Provide a fake frappe module so the import works without a Frappe instance.
frappe_mock = MagicMock()
frappe_mock.utils.flt = lambda x, *a, **kw: float(x or 0)
frappe_mock.utils.today = lambda: "2026-03-23"
frappe_mock.utils.round_based_on_smallest_currency_fraction = lambda x, *a, **kw: x
sys.modules.setdefault("frappe", frappe_mock)
sys.modules.setdefault("frappe.utils", frappe_mock.utils)

from procurement_ai.chain_builder.purchase_order import _ensure_supplier_link


class TestEnsureSupplierLink:
    """Test suite for _ensure_supplier_link."""

    def setup_method(self):
        frappe_mock.reset_mock()

    # ---- No-op cases ----

    def test_noop_when_no_supplier(self):
        """Should do nothing when supplier is empty."""
        _ensure_supplier_link("ITEM-001", "", "SUP-PART-1")
        frappe_mock.get_all.assert_not_called()

    def test_noop_when_no_item_code(self):
        """Should do nothing when item_code is empty."""
        _ensure_supplier_link("", "Supplier A", "SUP-PART-1")
        frappe_mock.get_all.assert_not_called()

    # ---- Supplier already linked with part number ----

    def test_existing_row_with_part_no_is_left_alone(self):
        """If the supplier row already has a supplier_part_no, don't touch it."""
        frappe_mock.get_all.return_value = [
            {"name": "row-1", "supplier_part_no": "EXISTING-CODE"}
        ]

        _ensure_supplier_link("ITEM-001", "Supplier A", "NEW-CODE")

        frappe_mock.get_all.assert_called_once()
        # Should NOT update the existing row
        frappe_mock.db.set_value.assert_not_called()
        # Should NOT append a new row
        frappe_mock.get_doc.assert_not_called()

    # ---- Supplier linked but missing part number ----

    def test_updates_blank_supplier_part_no(self):
        """If supplier row exists but has no part number, update it."""
        frappe_mock.get_all.return_value = [
            {"name": "row-1", "supplier_part_no": ""}
        ]

        _ensure_supplier_link("ITEM-001", "Supplier A", "NEW-CODE")

        frappe_mock.db.set_value.assert_called_once_with(
            "Item Supplier", "row-1", "supplier_part_no", "NEW-CODE"
        )

    def test_does_not_update_when_no_extracted_code(self):
        """If supplier row exists with blank part_no but we have no code either, skip."""
        frappe_mock.get_all.return_value = [
            {"name": "row-1", "supplier_part_no": ""}
        ]

        _ensure_supplier_link("ITEM-001", "Supplier A", "")

        frappe_mock.db.set_value.assert_not_called()

    # ---- Supplier not linked at all ----

    def test_appends_new_supplier_row_with_part_no(self):
        """If the supplier is not linked, create a new supplier_items row."""
        frappe_mock.get_all.return_value = []
        mock_doc = MagicMock()
        frappe_mock.get_doc.return_value = mock_doc

        _ensure_supplier_link("ITEM-001", "Supplier A", "SUP-PART-1")

        frappe_mock.get_doc.assert_called_once_with("Item", "ITEM-001")
        mock_doc.append.assert_called_once_with(
            "supplier_items",
            {"supplier": "Supplier A", "supplier_part_no": "SUP-PART-1"},
        )
        mock_doc.save.assert_called_once_with(ignore_permissions=True)

    def test_appends_new_supplier_row_without_part_no(self):
        """If no extracted code, still link the supplier (without part number)."""
        frappe_mock.get_all.return_value = []
        mock_doc = MagicMock()
        frappe_mock.get_doc.return_value = mock_doc

        _ensure_supplier_link("ITEM-001", "Supplier A", "")

        mock_doc.append.assert_called_once_with(
            "supplier_items",
            {"supplier": "Supplier A"},
        )
        mock_doc.save.assert_called_once_with(ignore_permissions=True)
