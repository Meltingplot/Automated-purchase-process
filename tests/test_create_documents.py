"""Tests for PO/PR/PI creation in purchase_order.py, purchase_receipt.py, purchase_invoice.py."""

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
    _build_items,
    _build_shipping_charges,
    _build_taxes,
    create_purchase_order,
)
from procurement_ai.chain_builder.purchase_receipt import create_purchase_receipt
from procurement_ai.chain_builder.purchase_invoice import create_purchase_invoice

SETTINGS = {"default_company": "Test Company", "auto_submit_documents": False}

EXTRACTED_DATA = {
    "document_type": "invoice",
    "supplier_name": "ACME GmbH",
    "document_date": "2024-01-15",
    "delivery_date": "2024-01-20",
    "order_reference": "PO-2024-001",
    "document_number": "RE-2024-001",
    "payment_terms": "30 Tage netto",
    "currency": "EUR",
    "items": [
        {"item_name": "Schrauben M8x50", "quantity": 100, "uom": "Stk", "unit_price": 0.15, "total_price": 15.00, "tax_rate": 19.0},
        {"item_name": "Muttern M8", "quantity": 100, "uom": "Stk", "unit_price": 0.10, "total_price": 10.00, "tax_rate": 19.0},
    ],
    "subtotal": 25.00,
    "tax_amount": 4.75,
    "total_amount": 29.75,
    "shipping_cost": 0.0,
    "discount_amount": None,
    "surcharge_amount": None,
}


def _reset():
    frappe_mock.reset_mock()
    po_mod._piece_uom_cache = None
    po_mod._uom_category_cache = None
    frappe_mock.db.exists.side_effect = None
    frappe_mock.db.exists.return_value = True
    frappe_mock.db.get_value.side_effect = None
    frappe_mock.db.get_value.return_value = None
    frappe_mock.db.get_single_value.side_effect = None
    frappe_mock.db.get_single_value.return_value = "Products"
    frappe_mock.get_all.side_effect = None
    frappe_mock.get_all.return_value = []
    frappe_mock.generate_hash.return_value = "ABCD1234"


def _setup_mock_doc(name="PO-TEST-001"):
    """Create a mock frappe doc returned by get_doc."""
    mock_doc = MagicMock()
    mock_doc.name = name
    frappe_mock.get_doc.return_value = mock_doc
    return mock_doc


# ============================================================
# create_purchase_order
# ============================================================


class TestCreatePurchaseOrder:
    def setup_method(self):
        _reset()

    def test_creates_po_with_items(self):
        mock_doc = _setup_mock_doc("PO-001")
        create_purchase_order(EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001")
        # Last get_doc call should be the PO itself
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["doctype"] == "Purchase Order"
        assert call_args["supplier"] == "ACME GmbH"
        assert len(call_args["items"]) == 2
        # insert called multiple times (item creation + PO itself)
        mock_doc.insert.assert_called_with(ignore_permissions=True)

    def test_po_sets_currency(self):
        _setup_mock_doc()
        create_purchase_order(EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001")
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["currency"] == "EUR"

    def test_po_stores_order_reference(self):
        _setup_mock_doc()
        create_purchase_order(EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001")
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["order_confirmation_no"] == "PO-2024-001"

    def test_po_applies_discount(self):
        _setup_mock_doc()
        data = {**EXTRACTED_DATA, "discount_amount": 5.0}
        create_purchase_order(data, "ACME GmbH", SETTINGS, "JOB-001")
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["apply_discount_on"] == "Net Total"
        assert call_args["discount_amount"] == 5.0

    def test_po_auto_submit(self):
        mock_doc = _setup_mock_doc()
        settings = {**SETTINGS, "auto_submit_documents": True}
        create_purchase_order(EXTRACTED_DATA, "ACME GmbH", settings, "JOB-001")
        mock_doc.submit.assert_called_once()

    def test_po_schedule_date_not_before_transaction(self):
        """schedule_date must be >= transaction_date."""
        _setup_mock_doc()
        data = {**EXTRACTED_DATA, "document_date": "2024-06-01", "delivery_date": "2024-01-01"}
        create_purchase_order(data, "ACME GmbH", SETTINGS, "JOB-001")
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["schedule_date"] >= call_args["transaction_date"]


# ============================================================
# _build_shipping_charges
# ============================================================


class TestBuildShippingCharges:
    def setup_method(self):
        _reset()

    def test_no_shipping(self):
        result = _build_shipping_charges({"shipping_cost": 0}, SETTINGS)
        assert result == []

    def test_with_shipping(self):
        frappe_mock.get_all.return_value = [{"name": "4730 - Bezugsnk"}]
        result = _build_shipping_charges({"shipping_cost": 5.99}, SETTINGS)
        assert len(result) == 1
        assert result[0]["charge_type"] == "Actual"
        assert result[0]["tax_amount"] == 5.99

    def test_no_shipping_account_returns_empty(self):
        frappe_mock.get_all.return_value = []
        frappe_mock.db.get_value.return_value = None
        result = _build_shipping_charges({"shipping_cost": 5.99}, SETTINGS)
        assert result == []


# ============================================================
# create_purchase_receipt
# ============================================================


class TestCreatePurchaseReceipt:
    def setup_method(self):
        _reset()
        frappe_mock.db.get_single_value.side_effect = lambda dt, field: {
            ("Stock Settings", "item_group"): "Products",
            ("Stock Settings", "default_warehouse"): "Stores - TC",
        }.get((dt, field))

    def test_creates_pr_with_items(self):
        mock_doc = _setup_mock_doc("PR-001")
        create_purchase_receipt(EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001")
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["doctype"] == "Purchase Receipt"
        assert len(call_args["items"]) == 2
        mock_doc.insert.assert_called_with(ignore_permissions=True)

    def test_pr_links_to_purchase_order(self):
        _setup_mock_doc()
        create_purchase_receipt(
            EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001",
            purchase_order="PO-001",
        )
        call_args = frappe_mock.get_doc.call_args[0][0]
        for item in call_args["items"]:
            assert item["purchase_order"] == "PO-001"

    def test_pr_uses_po_item_links(self):
        """PR items must use item_code from po_item_links, not re-resolve."""
        _setup_mock_doc()
        po_links = {
            0: {"name": "po-item-row-1", "item_code": "PO-BOLT"},
            1: {"name": "po-item-row-2", "item_code": "PO-NUT"},
        }
        create_purchase_receipt(
            EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001",
            purchase_order="PO-001", po_item_links=po_links,
        )
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["items"][0]["item_code"] == "PO-BOLT"
        assert call_args["items"][0]["purchase_order_item"] == "po-item-row-1"
        assert call_args["items"][1]["item_code"] == "PO-NUT"


# ============================================================
# create_purchase_invoice
# ============================================================


class TestCreatePurchaseInvoice:
    def setup_method(self):
        _reset()

    def test_creates_pi_with_items(self):
        mock_doc = _setup_mock_doc("PINV-001")
        create_purchase_invoice(EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001")
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["doctype"] == "Purchase Invoice"
        assert call_args["bill_no"] == "RE-2024-001"
        assert len(call_args["items"]) == 2
        mock_doc.insert.assert_called_with(ignore_permissions=True)

    def test_pi_links_to_po_and_pr(self):
        _setup_mock_doc()
        create_purchase_invoice(
            EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001",
            purchase_order="PO-001", purchase_receipt="PR-001",
        )
        call_args = frappe_mock.get_doc.call_args[0][0]
        for item in call_args["items"]:
            assert item["purchase_order"] == "PO-001"
            assert item["purchase_receipt"] == "PR-001"

    def test_pi_uses_po_and_pr_item_links(self):
        """PI items must use item_code from po/pr_item_links."""
        _setup_mock_doc()
        po_links = {0: {"name": "po-row-1", "item_code": "BOLT"}}
        pr_links = {0: {"name": "pr-row-1", "item_code": "BOLT"}}
        create_purchase_invoice(
            EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001",
            purchase_order="PO-001", purchase_receipt="PR-001",
            po_item_links=po_links, pr_item_links=pr_links,
        )
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["items"][0]["item_code"] == "BOLT"
        assert call_args["items"][0]["po_detail"] == "po-row-1"
        assert call_args["items"][0]["pr_detail"] == "pr-row-1"

    def test_pi_payment_terms_comment(self):
        mock_doc = _setup_mock_doc()
        create_purchase_invoice(EXTRACTED_DATA, "ACME GmbH", SETTINGS, "JOB-001")
        # Should include payment terms and retrospective comments
        # (also item creation comments from _create_item)
        comment_texts = [
            str(c) for c in mock_doc.add_comment.call_args_list
        ]
        has_payment = any("Payment terms" in t for t in comment_texts)
        has_retro = any("Retrospectively" in t for t in comment_texts)
        assert has_payment
        assert has_retro

    def test_pi_applies_discount(self):
        _setup_mock_doc()
        data = {**EXTRACTED_DATA, "discount_amount": 3.0}
        create_purchase_invoice(data, "ACME GmbH", SETTINGS, "JOB-001")
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["discount_amount"] == 3.0

    def test_pi_due_date_not_before_posting(self):
        """due_date must be >= posting_date."""
        _setup_mock_doc()
        data = {**EXTRACTED_DATA, "document_date": "2024-06-01", "delivery_date": "2024-01-01"}
        create_purchase_invoice(data, "ACME GmbH", SETTINGS, "JOB-001")
        call_args = frappe_mock.get_doc.call_args[0][0]
        assert call_args["due_date"] >= call_args["posting_date"]
