"""Tests for _verify_amounts currency handling in ingest.

Created documents are booked in the company base currency, which may differ
from the document's original currency. The verification must convert the
extracted total with the exchange rate at the document date before comparing,
so a foreign-currency document does not produce a false "amount mismatch"
that is really just the FX difference.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# Fake frappe + erpnext so ingest imports without a Frappe instance.
sys.modules.setdefault("frappe", MagicMock())
sys.modules.setdefault("frappe.utils", sys.modules["frappe"].utils)
sys.modules.setdefault("erpnext", MagicMock())
sys.modules.setdefault("erpnext.setup", sys.modules["erpnext"].setup)
sys.modules.setdefault("erpnext.setup.utils", sys.modules["erpnext"].setup.utils)

frappe_mock = sys.modules["frappe"]
erp_utils = sys.modules["erpnext.setup.utils"]

from procurement_ai.procurement_ai.api.ingest import _verify_amounts

CREATED = {
    "purchase_order": "PO-1",
    "purchase_receipt": "PR-1",
    "purchase_invoice": "PI-1",
}


def _doc(grand_total, currency):
    return types.SimpleNamespace(grand_total=grand_total, currency=currency)


def setup_function():
    frappe_mock.reset_mock()
    frappe_mock.utils.today = lambda: "2026-01-01"
    erp_utils.get_exchange_rate.side_effect = None


def test_no_total_returns_false():
    assert _verify_amounts({"total_amount": None}, CREATED) == (False, None)


def test_same_currency_within_tolerance():
    frappe_mock.db.get_value.return_value = _doc(100.00, "EUR")
    has_mismatch, msg = _verify_amounts(
        {"total_amount": 100.00, "currency": "EUR"}, CREATED, tolerance=0.05,
    )
    assert has_mismatch is False
    assert "✓" in msg


def test_same_currency_beyond_tolerance():
    frappe_mock.db.get_value.return_value = _doc(110.00, "EUR")
    has_mismatch, msg = _verify_amounts(
        {"total_amount": 100.00, "currency": "EUR"}, CREATED, tolerance=0.05,
    )
    assert has_mismatch is True
    assert "mismatch" in msg.lower()


def test_foreign_currency_converted_matches():
    # USD 456.45 booked as EUR 439.70 @ rate 0.9633 → no false mismatch
    frappe_mock.db.get_value.return_value = _doc(439.70, "EUR")
    erp_utils.get_exchange_rate.return_value = 0.9633
    has_mismatch, msg = _verify_amounts(
        {"total_amount": 456.45, "currency": "USD", "document_date": "2025-03-25"},
        CREATED, tolerance=0.05,
    )
    assert has_mismatch is False, msg
    # Rate fetched USD→EUR at the document date
    args, _ = erp_utils.get_exchange_rate.call_args
    assert args[0] == "USD" and args[1] == "EUR" and args[2] == "2025-03-25"


def test_foreign_currency_real_mismatch_still_flagged():
    # A genuine mismatch survives the conversion
    frappe_mock.db.get_value.return_value = _doc(400.00, "EUR")
    erp_utils.get_exchange_rate.return_value = 0.9633
    has_mismatch, _ = _verify_amounts(
        {"total_amount": 456.45, "currency": "USD", "document_date": "2025-03-25"},
        CREATED, tolerance=0.05,
    )
    assert has_mismatch is True
