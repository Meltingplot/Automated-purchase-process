"""Tests for the tax model: Tax-Rule-driven template selection + Bezugsnebenkosten charge rows.

VAT itself is delegated to ERPNext (per-item Item Tax Templates), so procurement_ai
only sets tax_category/taxes_and_charges and appends shipping/surcharge charges.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Provide a fake frappe module so the import works without a Frappe instance.
_new_mock = MagicMock()
_new_mock.utils.flt = lambda x, *a, **kw: float(x or 0)
_new_mock.utils.today = lambda: "2026-03-24"
_new_mock.utils.round_based_on_smallest_currency_fraction = lambda x, *a, **kw: x
frappe_mock = sys.modules.setdefault("frappe", _new_mock)
sys.modules.setdefault("frappe.utils", frappe_mock.utils)

from procurement_ai.chain_builder.purchase_order import (
    _apply_tax_template,
    _build_charge_rows,
)


def _reset_frappe():
    frappe_mock.reset_mock()
    frappe_mock.db.get_value.side_effect = None
    frappe_mock.db.get_value.return_value = None
    frappe_mock.get_all.side_effect = None
    frappe_mock.get_all.return_value = []


class TestApplyTaxTemplate:
    """tax_category + Tax-Rule-resolved template are written onto the doc data."""

    def setup_method(self):
        _reset_frappe()

    def test_sets_tax_category_from_supplier(self):
        frappe_mock.db.get_value.side_effect = lambda dt, name, field, **kw: (
            "Inland" if field == "tax_category" else None
        )
        doc_data = {}
        _apply_tax_template(doc_data, "ACME GmbH", "Test GmbH", "2026-03-24")
        assert doc_data["tax_category"] == "Inland"

    def test_no_tax_category_leaves_field_unset(self):
        frappe_mock.db.get_value.return_value = None
        doc_data = {}
        _apply_tax_template(doc_data, "ACME GmbH", "Test GmbH", "2026-03-24")
        assert "tax_category" not in doc_data

    def test_sets_resolved_template(self):
        """When the Tax Rule resolves a template, it is recorded on taxes_and_charges."""
        import types

        frappe_mock.db.get_value.return_value = None
        doc_data = {}
        # Inject a stub erpnext.accounts.party so the lazy import inside
        # _apply_tax_template succeeds and returns a template name.
        erpnext = types.ModuleType("erpnext")
        accounts = types.ModuleType("erpnext.accounts")
        party = types.ModuleType("erpnext.accounts.party")
        party.set_taxes = lambda *a, **kw: "Inland VAT 19%"
        accounts.party = party
        erpnext.accounts = accounts
        sys.modules["erpnext"] = erpnext
        sys.modules["erpnext.accounts"] = accounts
        sys.modules["erpnext.accounts.party"] = party
        try:
            _apply_tax_template(doc_data, "ACME GmbH", "Test GmbH", "2026-03-24")
        finally:
            for m in ("erpnext.accounts.party", "erpnext.accounts", "erpnext"):
                sys.modules.pop(m, None)
        assert doc_data["taxes_and_charges"] == "Inland VAT 19%"

    def test_tax_rule_failure_is_non_fatal(self):
        """No erpnext / no Tax Rule → no taxes_and_charges, but no error either."""
        frappe_mock.db.get_value.return_value = None
        sys.modules.pop("erpnext.accounts.party", None)
        doc_data = {}
        _apply_tax_template(doc_data, "ACME GmbH", "Test GmbH", "2026-03-24")
        assert "taxes_and_charges" not in doc_data


class TestBuildChargeRowsModel:
    """Shipping + surcharge are Actual Bezugsnebenkosten rows; no VAT rows."""

    def setup_method(self):
        _reset_frappe()

    def test_no_vat_rows_built(self):
        """We never emit rows with a 'rate' — VAT is ERPNext's job now."""
        frappe_mock.get_all.return_value = [{"name": "4730 - Bezugsnk"}]
        rows = _build_charge_rows(
            {"items": [{"tax_rate": 19.0}], "shipping_cost": 10.0}, {"default_company": "Test GmbH"}
        )
        assert all("rate" not in r for r in rows)
        assert all(r["charge_type"] == "Actual" for r in rows)

    def test_shipping_and_surcharge_valuation(self):
        frappe_mock.get_all.return_value = [{"name": "4730 - Bezugsnk"}]
        rows = _build_charge_rows(
            {"shipping_cost": 10.0, "surcharge_amount": 5.0}, {"default_company": "Test GmbH"}
        )
        assert len(rows) == 2
        for row in rows:
            assert row["category"] == "Valuation and Total"


class TestGetDefaultExpenseAccount:
    """Test suite for _get_default_expense_account fallback chain."""

    def setup_method(self):
        frappe_mock.reset_mock()

    def test_returns_company_default_when_set(self):
        from procurement_ai.chain_builder.purchase_invoice import (
            _get_default_expense_account,
        )

        frappe_mock.db.get_value.side_effect = None
        frappe_mock.db.get_value.return_value = "5000 - Herstellungskosten"
        result = _get_default_expense_account("Test GmbH")
        assert result == "5000 - Herstellungskosten"

    def test_prefers_cogs_over_generic_expense(self):
        """When no company default, prefer Cost of Goods Sold account type."""
        from procurement_ai.chain_builder.purchase_invoice import (
            _get_default_expense_account,
        )

        frappe_mock.db.get_value.return_value = None  # no company default
        frappe_mock.get_all.side_effect = [
            # Cost of Goods Sold accounts
            [{"name": "5800 - Aufwendungen für bezogene Waren"}],
        ]

        result = _get_default_expense_account("Test GmbH")
        assert result == "5800 - Aufwendungen für bezogene Waren"

    def test_falls_back_to_expense_account_type(self):
        """When no COGS account, fall back to Expense Account type."""
        from procurement_ai.chain_builder.purchase_invoice import (
            _get_default_expense_account,
        )

        frappe_mock.db.get_value.return_value = None
        frappe_mock.get_all.side_effect = [
            [],  # no Cost of Goods Sold accounts
            [{"name": "6300 - Sonstige Aufwendungen"}],  # Expense Account type
        ]

        result = _get_default_expense_account("Test GmbH")
        assert result == "6300 - Sonstige Aufwendungen"

    def test_last_resort_any_expense_account(self):
        """When no typed accounts exist, fall back to any non-group expense."""
        from procurement_ai.chain_builder.purchase_invoice import (
            _get_default_expense_account,
        )

        frappe_mock.db.get_value.return_value = None
        frappe_mock.get_all.side_effect = [
            [],  # no Cost of Goods Sold
            [],  # no Expense Account type
            [{"name": "5000 - Herstellungskosten"}],  # generic fallback
        ]

        result = _get_default_expense_account("Test GmbH")
        assert result == "5000 - Herstellungskosten"
