"""Tests for RetrospectiveChainBuilder and attachments."""

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

from procurement_ai.chain_builder.attachments import (
    ATTACHMENT_TARGETS,
    attach_source_to_chain,
)
from procurement_ai.chain_builder.retrospective import (
    NEEDED_DOCS,
    RetrospectiveChainBuilder,
)


def _reset():
    frappe_mock.reset_mock()
    frappe_mock.db.get_value.side_effect = None
    frappe_mock.db.get_value.return_value = None
    frappe_mock.get_all.side_effect = None
    frappe_mock.get_all.return_value = []
    # Company base currency matches the document currency (EUR), so
    # _convert_to_company_currency takes the base-currency path (no conversion).
    frappe_mock.get_cached_value.side_effect = None
    frappe_mock.get_cached_value.return_value = "EUR"


# ============================================================
# NEEDED_DOCS mapping
# ============================================================


class TestNeededDocs:
    def test_cart_needs_po_only(self):
        assert NEEDED_DOCS["Cart"] == ["Purchase Order"]

    def test_order_confirmation_needs_po_only(self):
        assert NEEDED_DOCS["Order Confirmation"] == ["Purchase Order"]

    def test_delivery_note_needs_po_and_pr(self):
        assert NEEDED_DOCS["Delivery Note"] == ["Purchase Order", "Purchase Receipt"]

    def test_invoice_needs_all_three(self):
        assert NEEDED_DOCS["Invoice"] == ["Purchase Order", "Purchase Receipt", "Purchase Invoice"]


# ============================================================
# ATTACHMENT_TARGETS mapping
# ============================================================


class TestAttachmentTargets:
    def test_cart_attaches_to_po(self):
        assert ATTACHMENT_TARGETS["Cart"]["primary"] == "Purchase Order"

    def test_invoice_primary_is_pi(self):
        assert ATTACHMENT_TARGETS["Invoice"]["primary"] == "Purchase Invoice"

    def test_invoice_secondary_includes_po_and_pr(self):
        secondary = ATTACHMENT_TARGETS["Invoice"]["secondary"]
        assert "Purchase Order" in secondary
        assert "Purchase Receipt" in secondary

    def test_delivery_note_secondary_includes_po(self):
        assert "Purchase Order" in ATTACHMENT_TARGETS["Delivery Note"]["secondary"]


# ============================================================
# attach_source_to_chain
# ============================================================


class TestAttachSourceToChain:
    def setup_method(self):
        _reset()

    def test_attaches_to_correct_targets(self):
        frappe_mock.get_all.return_value = [{"file_name": "invoice.pdf", "is_private": 1}]
        mock_doc = MagicMock()
        mock_doc.name = "FILE-001"
        frappe_mock.get_doc.return_value = mock_doc

        results = attach_source_to_chain(
            source_file_url="/files/invoice.pdf",
            source_type="Invoice",
            created_docs={
                "purchase_order": "PO-001",
                "purchase_receipt": "PR-001",
                "purchase_invoice": "PINV-001",
            },
            job_name="JOB-001",
        )
        # Should attach to PI (primary) + PO + PR (secondary) = 3 results
        assert len(results) == 3

    def test_no_file_record_returns_empty(self):
        frappe_mock.get_all.return_value = []
        results = attach_source_to_chain(
            source_file_url="/files/missing.pdf",
            source_type="Invoice",
            created_docs={"purchase_invoice": "PINV-001"},
            job_name="JOB-001",
        )
        assert results == []

    def test_unknown_source_type_returns_empty(self):
        results = attach_source_to_chain(
            source_file_url="/files/test.pdf",
            source_type="Unknown Type",
            created_docs={},
            job_name="JOB-001",
        )
        assert results == []

    def test_skips_missing_doc_targets(self):
        """If a target doc wasn't created, skip it."""
        frappe_mock.get_all.return_value = [{"file_name": "inv.pdf", "is_private": 1}]
        mock_doc = MagicMock()
        mock_doc.name = "FILE-001"
        frappe_mock.get_doc.return_value = mock_doc

        results = attach_source_to_chain(
            source_file_url="/files/inv.pdf",
            source_type="Invoice",
            created_docs={"purchase_invoice": "PINV-001"},  # PO and PR missing
            job_name="JOB-001",
        )
        assert len(results) == 1
        assert results[0]["doctype"] == "Purchase Invoice"


# ============================================================
# RetrospectiveChainBuilder.build_chain (integration)
# ============================================================

EXTRACTED_DATA = {
    "document_type": "invoice",
    "supplier_name": "ACME GmbH",
    "supplier_address": "Musterstr. 1, 12345 Berlin",
    "supplier_tax_id": "DE123456789",
    "supplier_email": "info@acme.de",
    "supplier_phone": "030 1234567",
    "document_number": "RE-2024-001",
    "document_date": "2024-01-15",
    "delivery_date": "2024-01-20",
    "order_reference": "PO-2024-001",
    "payment_terms": "30 Tage netto",
    "currency": "EUR",
    "items": [
        {"item_name": "Schrauben M8x50", "quantity": 100, "uom": "Stk",
         "unit_price": 0.15, "total_price": 15.00},
    ],
    "subtotal": 15.00,
    "tax_amount": 2.85,
    "total_amount": 17.85,
    "shipping_cost": 0.0,
}

SETTINGS = {"default_company": "Test Company", "auto_submit_documents": False}


class TestBuildChain:
    def setup_method(self):
        _reset()

    @patch("procurement_ai.chain_builder.retrospective.attach_source_to_chain")
    @patch("procurement_ai.chain_builder.retrospective.create_purchase_invoice")
    @patch("procurement_ai.chain_builder.retrospective.create_purchase_receipt")
    @patch("procurement_ai.chain_builder.retrospective.create_purchase_order")
    @patch("procurement_ai.chain_builder.retrospective.build_item_links")
    @patch("procurement_ai.chain_builder.retrospective.find_matching_purchase_invoice")
    @patch("procurement_ai.chain_builder.retrospective.find_matching_purchase_receipt")
    @patch("procurement_ai.chain_builder.retrospective.find_matching_purchase_order")
    @patch("procurement_ai.chain_builder.retrospective.ensure_supplier")
    def test_invoice_creates_all_three(
        self, mock_supplier, mock_find_po, mock_find_pr, mock_find_pi,
        mock_build_links, mock_create_po, mock_create_pr, mock_create_pi,
        mock_attach,
    ):
        """Invoice source type should create PO + PR + PI."""
        mock_supplier.return_value = "ACME GmbH"
        # No existing matches
        from procurement_ai.chain_builder.document_matcher import DocumentMatch
        mock_find_po.return_value = DocumentMatch(found=False)
        mock_find_pr.return_value = DocumentMatch(found=False)
        mock_find_pi.return_value = DocumentMatch(found=False)
        mock_build_links.return_value = {}
        mock_create_po.return_value = "PO-001"
        mock_create_pr.return_value = "PR-001"
        mock_create_pi.return_value = "PINV-001"
        mock_attach.return_value = []

        builder = RetrospectiveChainBuilder()
        result = builder.build_chain(
            EXTRACTED_DATA, "Invoice", "/files/test.pdf", SETTINGS, "JOB-001",
        )
        assert result["purchase_order"] == "PO-001"
        assert result["purchase_receipt"] == "PR-001"
        assert result["purchase_invoice"] == "PINV-001"
        assert result["purchase_order_matched"] is False
        assert result["purchase_receipt_matched"] is False
        assert result["purchase_invoice_matched"] is False

    @patch("procurement_ai.chain_builder.retrospective.attach_source_to_chain")
    @patch("procurement_ai.chain_builder.retrospective.create_purchase_order")
    @patch("procurement_ai.chain_builder.retrospective.build_item_links")
    @patch("procurement_ai.chain_builder.retrospective.find_matching_purchase_order")
    @patch("procurement_ai.chain_builder.retrospective.ensure_supplier")
    def test_cart_creates_po_only(
        self, mock_supplier, mock_find_po, mock_build_links,
        mock_create_po, mock_attach,
    ):
        """Cart source type should create only PO."""
        mock_supplier.return_value = "ACME GmbH"
        from procurement_ai.chain_builder.document_matcher import DocumentMatch
        mock_find_po.return_value = DocumentMatch(found=False)
        mock_build_links.return_value = {}
        mock_create_po.return_value = "PO-001"
        mock_attach.return_value = []

        builder = RetrospectiveChainBuilder()
        result = builder.build_chain(
            EXTRACTED_DATA, "Cart", "/files/test.pdf", SETTINGS, "JOB-001",
        )
        assert "purchase_order" in result
        assert "purchase_receipt" not in result
        assert "purchase_invoice" not in result

    @patch("procurement_ai.chain_builder.retrospective.attach_source_to_chain")
    @patch("procurement_ai.chain_builder.retrospective.find_matching_purchase_order")
    @patch("procurement_ai.chain_builder.retrospective.ensure_supplier")
    def test_matched_po_not_recreated(
        self, mock_supplier, mock_find_po, mock_attach,
    ):
        """Existing PO match should return matched=True, not create new."""
        mock_supplier.return_value = "ACME GmbH"
        from procurement_ai.chain_builder.document_matcher import DocumentMatch
        mock_find_po.return_value = DocumentMatch(
            found=True, doc_name="EXISTING-PO",
            match_confidence=1.0, match_method="po_name_exact",
            item_links={},
        )

        builder = RetrospectiveChainBuilder()
        result = builder.build_chain(
            EXTRACTED_DATA, "Cart", "", SETTINGS, "JOB-001",
        )
        assert result["purchase_order"] == "EXISTING-PO"
        assert result["purchase_order_matched"] is True
        assert result["purchase_order_match_method"] == "po_name_exact"


# ============================================================
# Currency conversion (_convert_to_company_currency)
# ============================================================

# Fake erpnext.setup.utils so the lazy `get_exchange_rate` import resolves.
_erpnext_mock = MagicMock()
sys.modules.setdefault("erpnext", _erpnext_mock)
sys.modules.setdefault("erpnext.setup", _erpnext_mock.setup)
sys.modules.setdefault("erpnext.setup.utils", _erpnext_mock.setup.utils)


class TestConvertToCompanyCurrency:
    def setup_method(self):
        _reset()
        # Company books in EUR
        frappe_mock.get_cached_value.return_value = "EUR"
        self.get_rate = sys.modules["erpnext.setup.utils"].get_exchange_rate
        self.get_rate.side_effect = None
        frappe_mock.throw.side_effect = None

    def _usd_data(self):
        return {
            "currency": "USD",
            "document_date": "2025-03-25",
            "items": [
                {"item_name": "Widget", "quantity": 2,
                 "unit_price": 100.0, "total_price": 200.0, "tax_rate": 19},
            ],
            "subtotal": 200.0,
            "tax_amount": 0.0,
            "total_amount": 200.0,
            "shipping_cost": 50.0,
            "discount_amount": None,
            "surcharge_amount": None,
        }

    def test_base_currency_is_noop(self):
        from procurement_ai.chain_builder.retrospective import (
            _convert_to_company_currency,
        )
        data = {"currency": "EUR", "total_amount": 100.0,
                "items": [{"unit_price": 50.0, "total_price": 100.0}]}
        out = _convert_to_company_currency(data, SETTINGS)
        assert out["currency"] == "EUR"
        assert out["total_amount"] == 100.0
        assert "_currency_note" not in out

    def test_foreign_currency_converts_amounts(self):
        from procurement_ai.chain_builder.retrospective import (
            _convert_to_company_currency,
        )
        self.get_rate.return_value = 0.9
        out = _convert_to_company_currency(self._usd_data(), SETTINGS)
        assert out["currency"] == "EUR"
        assert out["total_amount"] == 180.0       # 200 * 0.9
        assert out["subtotal"] == 180.0
        assert out["shipping_cost"] == 45.0       # 50 * 0.9
        assert out["items"][0]["unit_price"] == 90.0    # 100 * 0.9
        assert out["items"][0]["total_price"] == 180.0
        # Percentages stay untouched
        assert out["items"][0]["tax_rate"] == 19
        # Audit metadata
        assert out["_original_currency"] == "USD"
        assert out["_original_total"] == 200.0
        assert out["_conversion_rate"] == 0.9
        assert "USD" in out["_currency_note"]

    def test_rate_fetched_at_document_date(self):
        from procurement_ai.chain_builder.retrospective import (
            _convert_to_company_currency,
        )
        self.get_rate.return_value = 0.9
        _convert_to_company_currency(self._usd_data(), SETTINGS)
        args, kwargs = self.get_rate.call_args
        assert args[0] == "USD"
        assert args[1] == "EUR"
        assert args[2] == "2025-03-25"   # document_date, not today()

    def test_missing_rate_raises(self):
        from procurement_ai.chain_builder.retrospective import (
            _convert_to_company_currency,
        )
        self.get_rate.return_value = 0
        frappe_mock.throw.side_effect = RuntimeError("no rate")
        with pytest.raises(RuntimeError):
            _convert_to_company_currency(self._usd_data(), SETTINGS)
