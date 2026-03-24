"""Tests for _get_tax_account — rate-aware tax account lookup."""

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

from procurement_ai.chain_builder.purchase_order import _get_tax_account, _build_taxes


class TestGetTaxAccount:
    """Test suite for rate-aware _get_tax_account."""

    def setup_method(self):
        frappe_mock.reset_mock()

    def test_returns_none_for_empty_company(self):
        assert _get_tax_account("", 19.0) is None
        assert _get_tax_account(None, 19.0) is None

    def test_matches_rate_from_default_template(self):
        """Should return the account whose rate matches in the default template."""
        frappe_mock.db.get_value.return_value = "Default VAT Template"
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
        frappe_mock.db.get_value.return_value = "Default VAT Template"
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
        frappe_mock.db.get_value.return_value = "Default VAT Template"
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
        frappe_mock.db.get_value.return_value = "Default VAT Template"
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
        frappe_mock.db.get_value.return_value = None
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
