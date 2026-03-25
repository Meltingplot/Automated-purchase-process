"""Tests for document matching in document_matcher.py."""

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

from procurement_ai.chain_builder.document_matcher import (
    DocumentMatch,
    _build_item_links_from_doc,
    _match_pi_by_amount_and_date,
    _match_pi_by_bill_no,
    _match_pi_by_purchase_order,
    _match_po_by_confirmation_no,
    _match_po_by_name,
    _match_pr_by_purchase_order,
    build_item_links,
    find_matching_purchase_invoice,
    find_matching_purchase_order,
    find_matching_purchase_receipt,
)

SETTINGS = {"default_company": "Test Company"}
EXTRACTED_DATA = {
    "order_reference": "PO-2024-001",
    "document_number": "RE-2024-001",
    "document_date": "2024-01-15",
    "total_amount": 29.75,
    "items": [
        {"item_code": "BOLT-M8", "item_name": "Schrauben M8x50", "quantity": 100, "unit_price": 0.15},
        {"item_code": "NUT-M8", "item_name": "Muttern M8", "quantity": 100, "unit_price": 0.10},
    ],
}


def _reset():
    frappe_mock.reset_mock()
    frappe_mock.db.get_value.side_effect = None
    frappe_mock.db.get_value.return_value = None
    frappe_mock.get_all.side_effect = None
    frappe_mock.get_all.return_value = []


# ============================================================
# _match_po_by_name
# ============================================================


class TestMatchPoByName:
    def setup_method(self):
        _reset()

    def test_exact_match(self):
        frappe_mock.db.get_value.return_value = MagicMock(name="PO-2024-001", docstatus=0)
        result = _match_po_by_name("PO-2024-001")
        assert result.found is True
        assert result.match_confidence == 1.0
        assert result.match_method == "po_name_exact"

    def test_no_match(self):
        frappe_mock.db.get_value.return_value = None
        result = _match_po_by_name("NONEXISTENT")
        assert result.found is False


# ============================================================
# _match_po_by_confirmation_no
# ============================================================


class TestMatchPoByConfirmationNo:
    def setup_method(self):
        _reset()

    def test_match(self):
        frappe_mock.db.get_value.return_value = MagicMock(name="PO-001", docstatus=1)
        result = _match_po_by_confirmation_no("AB-12345", "ACME GmbH")
        assert result.found is True
        assert result.match_confidence == 0.95
        assert result.match_method == "po_confirmation_no"

    def test_no_match(self):
        frappe_mock.db.get_value.return_value = None
        result = _match_po_by_confirmation_no("NOPE", "ACME GmbH")
        assert result.found is False


# ============================================================
# _match_pr_by_purchase_order
# ============================================================


class TestMatchPrByPurchaseOrder:
    def setup_method(self):
        _reset()

    def test_linked_pr_found(self):
        frappe_mock.get_all.side_effect = [
            [{"parent": "PR-001"}],  # Purchase Receipt Item query
            [],  # _build_item_links_from_doc
        ]
        frappe_mock.db.get_value.return_value = 1  # docstatus (not cancelled)
        result = _match_pr_by_purchase_order("PO-001")
        assert result.found is True
        assert result.match_confidence == 0.95
        assert result.match_method == "pr_linked_to_po"

    def test_no_linked_pr(self):
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = []
        result = _match_pr_by_purchase_order("PO-001")
        assert result.found is False

    def test_cancelled_pr_rejected(self):
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = [{"parent": "PR-001"}]
        frappe_mock.db.get_value.return_value = 2  # cancelled
        result = _match_pr_by_purchase_order("PO-001")
        assert result.found is False


# ============================================================
# _match_pi_by_bill_no
# ============================================================


class TestMatchPiByBillNo:
    def setup_method(self):
        _reset()

    def test_match(self):
        mock_pi = MagicMock()
        mock_pi.name = "PINV-001"
        mock_pi.docstatus = 0
        frappe_mock.db.get_value.return_value = mock_pi
        result = _match_pi_by_bill_no("RE-2024-001", "ACME GmbH")
        assert result.found is True
        assert result.match_confidence == 1.0
        assert result.match_method == "bill_no"

    def test_no_match(self):
        frappe_mock.db.get_value.return_value = None
        result = _match_pi_by_bill_no("NOPE", "ACME GmbH")
        assert result.found is False


# ============================================================
# _match_pi_by_purchase_order
# ============================================================


class TestMatchPiByPurchaseOrder:
    def setup_method(self):
        _reset()

    def test_linked_pi_found(self):
        frappe_mock.get_all.side_effect = [
            [{"parent": "PINV-001"}],  # PI Item query
            [],  # _build_item_links_from_doc
        ]
        frappe_mock.db.get_value.return_value = 0  # docstatus
        result = _match_pi_by_purchase_order("PO-001")
        assert result.found is True
        assert result.match_confidence == 0.90
        assert result.match_method == "pi_linked_to_po"

    def test_no_linked_pi(self):
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = []
        result = _match_pi_by_purchase_order("PO-001")
        assert result.found is False


# ============================================================
# _match_pi_by_amount_and_date
# ============================================================


class TestMatchPiByAmountAndDate:
    def setup_method(self):
        _reset()

    def _make_candidate(self, name, grand_total, posting_date="2024-01-15"):
        c = MagicMock()
        c.name = name
        c.docstatus = 0
        c.posting_date = posting_date
        c.grand_total = grand_total
        return c

    def test_match_within_5_percent(self):
        frappe_mock.get_all.side_effect = [
            [self._make_candidate("PINV-001", 29.75)],  # candidates
            [],  # _build_item_links_from_doc
        ]
        data = {"total_amount": 29.75, "document_date": "2024-01-15"}
        result = _match_pi_by_amount_and_date("ACME GmbH", data)
        assert result.found is True
        assert result.match_method == "pi_amount_date"

    def test_no_match_outside_5_percent(self):
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = [self._make_candidate("PINV-001", 100.00)]
        data = {"total_amount": 29.75, "document_date": "2024-01-15"}
        result = _match_pi_by_amount_and_date("ACME GmbH", data)
        assert result.found is False

    def test_no_supplier(self):
        result = _match_pi_by_amount_and_date("", {"total_amount": 29.75})
        assert result.found is False

    def test_no_total_amount(self):
        result = _match_pi_by_amount_and_date("ACME GmbH", {})
        assert result.found is False

    def test_ambiguous_match_rejected(self):
        """Top-2 within 0.10 → no match."""
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = [
            self._make_candidate("PINV-001", 29.75, "2024-01-15"),
            self._make_candidate("PINV-002", 29.50, "2024-01-15"),
        ]
        data = {"total_amount": 29.75, "document_date": "2024-01-15"}
        result = _match_pi_by_amount_and_date("ACME GmbH", data)
        assert result.found is False


# ============================================================
# _build_item_links_from_doc
# ============================================================


class TestBuildItemLinksFromDoc:
    def setup_method(self):
        _reset()

    def test_builds_links(self):
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = [
            {"name": "row-1", "item_code": "BOLT-M8"},
            {"name": "row-2", "item_code": "NUT-M8"},
        ]
        links = _build_item_links_from_doc("Purchase Order", "PO-001")
        assert "BOLT-M8" in links
        assert links["BOLT-M8"]["name"] == "row-1"
        assert "NUT-M8" in links

    def test_unknown_doctype(self):
        links = _build_item_links_from_doc("Unknown Doctype", "X")
        assert links == {}


# ============================================================
# find_matching_purchase_order (integration)
# ============================================================


class TestFindMatchingPurchaseOrder:
    def setup_method(self):
        _reset()

    def test_priority1_exact_name(self):
        """Order reference matching PO name takes highest priority."""
        mock_po = MagicMock()
        mock_po.name = "PO-2024-001"
        mock_po.docstatus = 0
        frappe_mock.db.get_value.return_value = mock_po
        result = find_matching_purchase_order("ACME GmbH", EXTRACTED_DATA, SETTINGS)
        assert result.found is True
        assert result.match_method == "po_name_exact"

    def test_no_match_returns_not_found(self):
        frappe_mock.db.get_value.return_value = None
        result = find_matching_purchase_order("ACME GmbH", EXTRACTED_DATA, SETTINGS)
        assert result.found is False


# ============================================================
# find_matching_purchase_receipt (integration)
# ============================================================


class TestFindMatchingPurchaseReceipt:
    def setup_method(self):
        _reset()

    def test_priority1_linked_to_po(self):
        frappe_mock.get_all.side_effect = [
            [{"parent": "PR-001"}],  # PR Item query
            [],  # _build_item_links_from_doc
        ]
        frappe_mock.db.get_value.return_value = 0
        result = find_matching_purchase_receipt(
            "ACME GmbH", EXTRACTED_DATA, SETTINGS, purchase_order="PO-001"
        )
        assert result.found is True
        assert result.match_method == "pr_linked_to_po"

    def test_no_match(self):
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = []
        result = find_matching_purchase_receipt("ACME GmbH", EXTRACTED_DATA, SETTINGS)
        assert result.found is False


# ============================================================
# find_matching_purchase_invoice (integration)
# ============================================================


class TestFindMatchingPurchaseInvoice:
    def setup_method(self):
        _reset()

    def test_priority1_bill_no(self):
        mock_pi = MagicMock()
        mock_pi.name = "PINV-001"
        mock_pi.docstatus = 0
        frappe_mock.db.get_value.return_value = mock_pi
        result = find_matching_purchase_invoice(
            "ACME GmbH", EXTRACTED_DATA, SETTINGS
        )
        assert result.found is True
        assert result.match_method == "bill_no"

    def test_priority2_linked_to_po(self):
        """bill_no returns None, then PO link found."""
        frappe_mock.db.get_value.side_effect = [
            None,  # _match_pi_by_bill_no
            0,     # docstatus for linked PI
        ]
        frappe_mock.get_all.side_effect = [
            [{"parent": "PINV-002"}],  # PI Item query
            [],  # _build_item_links_from_doc
        ]
        result = find_matching_purchase_invoice(
            "ACME GmbH", EXTRACTED_DATA, SETTINGS, purchase_order="PO-001"
        )
        assert result.found is True
        assert result.match_method == "pi_linked_to_po"

    def test_no_match(self):
        frappe_mock.db.get_value.return_value = None
        frappe_mock.get_all.side_effect = None
        frappe_mock.get_all.return_value = []
        result = find_matching_purchase_invoice(
            "ACME GmbH", EXTRACTED_DATA, SETTINGS
        )
        assert result.found is False
