"""Tests for _get_tax_account / _pick_input_tax_account — rate-aware tax account lookup."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# Provide a fake frappe module so the import works without a Frappe instance.
# Use setdefault and capture the actual module-level object (may already exist
# from another test file that was imported first).
_new_mock = MagicMock()
_new_mock.utils.flt = lambda x, *a, **kw: float(x or 0)
_new_mock.utils.today = lambda: "2026-03-24"
_new_mock.utils.round_based_on_smallest_currency_fraction = lambda x, *a, **kw: x
frappe_mock = sys.modules.setdefault("frappe", _new_mock)
sys.modules.setdefault("frappe.utils", frappe_mock.utils)

from procurement_ai.chain_builder.purchase_order import (
    _get_tax_account,
    _build_taxes,
    _pick_input_tax_account,
)


def _make_db_get_value(template_name, root_type_map=None):
    """Build a side_effect for frappe.db.get_value that handles both
    template lookups and Account root_type lookups.

    *template_name* is returned for the Purchase Taxes and Charges Template query.
    *root_type_map* maps account names to root_type values (default: all Asset).
    """
    if root_type_map is None:
        root_type_map = {}

    def _side_effect(doctype, filters_or_name, field=None, **kw):
        if doctype == "Purchase Taxes and Charges Template":
            return template_name
        if doctype == "Account":
            # filters_or_name is the account name, field is "root_type"
            return root_type_map.get(filters_or_name, "Asset")
        return None

    return _side_effect


class TestPickInputTaxAccount:
    """Test suite for the Vorsteuer/Umsatzsteuer preference helper."""

    def setup_method(self):
        frappe_mock.reset_mock()

    def test_prefers_vorsteuer_over_umsatzsteuer(self):
        """When both Vorsteuer (Asset) and Umsatzsteuer (Liability) match the
        same rate, Vorsteuer must be selected for purchase documents."""
        rows = [
            {"account_head": "3806 - Umsatzsteuer 19%", "rate": 19.0},
            {"account_head": "1406 - Vorsteuer 19%", "rate": 19.0},
        ]
        frappe_mock.db.get_value.side_effect = lambda dt, name, field, **kw: {
            "3806 - Umsatzsteuer 19%": "Liability",
            "1406 - Vorsteuer 19%": "Asset",
        }.get(name)

        result = _pick_input_tax_account(rows, 19.0, 0.01)
        assert result == "1406 - Vorsteuer 19%"

    def test_falls_back_to_umsatzsteuer_when_no_vorsteuer(self):
        """When only Umsatzsteuer exists for the rate, still return it (with warning)."""
        rows = [
            {"account_head": "3806 - Umsatzsteuer 19%", "rate": 19.0},
        ]
        frappe_mock.db.get_value.side_effect = lambda dt, name, field, **kw: "Liability"

        result = _pick_input_tax_account(rows, 19.0, 0.01)
        assert result == "3806 - Umsatzsteuer 19%"

    def test_returns_none_when_no_rate_match(self):
        rows = [
            {"account_head": "1406 - Vorsteuer 19%", "rate": 19.0},
        ]
        result = _pick_input_tax_account(rows, 7.0, 0.01)
        assert result is None

    def test_skips_non_matching_rates(self):
        """Only accounts matching the requested rate should be considered."""
        rows = [
            {"account_head": "3806 - Umsatzsteuer 19%", "rate": 19.0},
            {"account_head": "1571 - Vorsteuer 7%", "rate": 7.0},
        ]
        frappe_mock.db.get_value.side_effect = lambda dt, name, field, **kw: {
            "3806 - Umsatzsteuer 19%": "Liability",
            "1571 - Vorsteuer 7%": "Asset",
        }.get(name, "Asset")

        # Requesting 19% — only "3806 - Umsatzsteuer 19%" matches
        result = _pick_input_tax_account(rows, 19.0, 0.01)
        assert result == "3806 - Umsatzsteuer 19%"


class TestGetTaxAccount:
    """Test suite for rate-aware _get_tax_account."""

    def setup_method(self):
        frappe_mock.reset_mock()

    def test_returns_none_for_empty_company(self):
        assert _get_tax_account("", 19.0) is None
        assert _get_tax_account(None, 19.0) is None

    def test_matches_rate_from_default_template(self):
        """Should return the account whose rate matches in the default template."""
        frappe_mock.db.get_value.side_effect = _make_db_get_value("Default VAT Template")
        frappe_mock.get_all.side_effect = [
            # Default template rows
            [
                {"account_head": "1571 - Vorsteuer 7%", "rate": 7.0},
                {"account_head": "1576 - Vorsteuer 19%", "rate": 19.0},
            ],
        ]

        result = _get_tax_account("Test Company", 19.0)
        assert result == "1576 - Vorsteuer 19%"

    def test_matches_7_percent_rate(self):
        """Should return the 7% account when 7% rate is requested."""
        frappe_mock.db.get_value.side_effect = _make_db_get_value("Default VAT Template")
        frappe_mock.get_all.side_effect = [
            # Default template rows
            [
                {"account_head": "1571 - Vorsteuer 7%", "rate": 7.0},
                {"account_head": "1576 - Vorsteuer 19%", "rate": 19.0},
            ],
        ]

        result = _get_tax_account("Test Company", 7.0)
        assert result == "1571 - Vorsteuer 7%"

    def test_returns_none_when_rate_not_in_any_template(self):
        """Must return None (not a random account) if no template has the rate."""
        frappe_mock.db.get_value.side_effect = _make_db_get_value("Default VAT Template")
        # Default template only has 19%
        frappe_mock.get_all.side_effect = [
            [{"account_head": "1576 - Vorsteuer 19%", "rate": 19.0}],
            # No other templates
            [],
        ]

        result = _get_tax_account("Test Company", 7.0)
        assert result is None

    def test_falls_back_to_other_templates(self):
        """When default template doesn't have the rate, check other templates."""
        frappe_mock.db.get_value.side_effect = _make_db_get_value("Default VAT Template")
        frappe_mock.get_all.side_effect = [
            # Default template rows — only 19%
            [{"account_head": "1576 - Vorsteuer 19%", "rate": 19.0}],
            # All templates for this company
            [{"name": "Default VAT Template"}, {"name": "Reduced VAT Template"}],
            # Reduced VAT Template rows
            [{"account_head": "1571 - Vorsteuer 7%", "rate": 7.0}],
        ]

        result = _get_tax_account("Test Company", 7.0)
        assert result == "1571 - Vorsteuer 7%"

    def test_no_default_template_falls_back_to_other(self):
        """When no default template exists, check any template for the company."""
        frappe_mock.db.get_value.side_effect = _make_db_get_value(None)
        frappe_mock.get_all.side_effect = [
            # All templates for this company
            [{"name": "Some VAT Template"}],
            # Template rows
            [
                {"account_head": "1571 - Vorsteuer 7%", "rate": 7.0},
                {"account_head": "1576 - Vorsteuer 19%", "rate": 19.0},
            ],
        ]

        result = _get_tax_account("Test Company", 19.0)
        assert result == "1576 - Vorsteuer 19%"

    def test_prefers_vorsteuer_in_mixed_template(self):
        """Template with both Umsatzsteuer and Vorsteuer at 19% → pick Vorsteuer."""
        frappe_mock.db.get_value.side_effect = _make_db_get_value(
            "Default VAT Template",
            root_type_map={
                "3806 - Umsatzsteuer 19%": "Liability",
                "1406 - Vorsteuer 19%": "Asset",
            },
        )
        frappe_mock.get_all.side_effect = [
            [
                {"account_head": "3806 - Umsatzsteuer 19%", "rate": 19.0},
                {"account_head": "1406 - Vorsteuer 19%", "rate": 19.0},
            ],
        ]

        result = _get_tax_account("Test Company", 19.0)
        assert result == "1406 - Vorsteuer 19%"

    def test_returns_umsatzsteuer_when_only_option(self):
        """When only Umsatzsteuer exists for the rate, still return it."""
        frappe_mock.db.get_value.side_effect = _make_db_get_value(
            "Default VAT Template",
            root_type_map={"3806 - Umsatzsteuer 19%": "Liability"},
        )
        frappe_mock.get_all.side_effect = [
            [{"account_head": "3806 - Umsatzsteuer 19%", "rate": 19.0}],
        ]

        result = _get_tax_account("Test Company", 19.0)
        assert result == "3806 - Umsatzsteuer 19%"


class TestBuildTaxesRateAwareness:
    """Verify _build_taxes creates separate rows with correct accounts per rate."""

    def setup_method(self):
        frappe_mock.reset_mock()

    def test_different_accounts_for_different_rates(self):
        """Items with 7% and 19% should produce two tax rows with distinct accounts."""
        extracted_data = {
            "items": [
                {"tax_rate": 19.0, "item_name": "Widget"},
                {"tax_rate": 7.0, "item_name": "Book"},
            ],
        }
        settings = {"default_company": "Test GmbH"}

        # Mock _get_tax_account to return rate-specific accounts
        with patch(
            "procurement_ai.chain_builder.purchase_order._get_tax_account"
        ) as mock_tax:
            mock_tax.side_effect = lambda company, rate: {
                7.0: "1571 - Vorsteuer 7%",
                19.0: "1576 - Vorsteuer 19%",
            }.get(rate)

            taxes = _build_taxes(extracted_data, settings)

        vat_rows = [t for t in taxes if "VAT" in t.get("description", "")]
        assert len(vat_rows) == 2
        assert vat_rows[0]["account_head"] == "1571 - Vorsteuer 7%"
        assert vat_rows[0]["rate"] == 7.0
        assert vat_rows[1]["account_head"] == "1576 - Vorsteuer 19%"
        assert vat_rows[1]["rate"] == 19.0

    def test_skips_rate_with_no_matching_account(self):
        """If no account is found for a rate, that tax row should be skipped."""
        extracted_data = {
            "items": [
                {"tax_rate": 19.0, "item_name": "Widget"},
                {"tax_rate": 5.0, "item_name": "Unknown rate item"},
            ],
        }
        settings = {"default_company": "Test GmbH"}

        with patch(
            "procurement_ai.chain_builder.purchase_order._get_tax_account"
        ) as mock_tax:
            # Only 19% has an account, 5% does not
            mock_tax.side_effect = lambda company, rate: (
                "1576 - Vorsteuer 19%" if abs(rate - 19.0) < 0.01 else None
            )

            taxes = _build_taxes(extracted_data, settings)

        vat_rows = [t for t in taxes if "VAT" in t.get("description", "")]
        assert len(vat_rows) == 1
        assert vat_rows[0]["rate"] == 19.0


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
