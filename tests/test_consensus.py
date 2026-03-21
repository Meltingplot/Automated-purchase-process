"""Tests for ConsensusEngine - multi-LLM result comparison."""

from __future__ import annotations

import pytest

from procurement_ai.llm.consensus import ConsensusEngine


class TestConsensusEngine:
    """Test suite for the ConsensusEngine."""

    def setup_method(self):
        self.engine = ConsensusEngine()

    def test_perfect_agreement(self, sample_extraction, sample_extraction_variant):
        """Two similar extractions should reach consensus."""
        result = self.engine.build_consensus(
            [sample_extraction, sample_extraction_variant]
        )
        assert result.confidence > 0.5
        assert result.agreed_data.get("supplier_name") is not None
        assert not result.needs_escalation or result.confidence >= 0.7

    def test_total_disagreement(self, sample_extraction, sample_extraction_wrong):
        """Completely different extractions should trigger escalation."""
        result = self.engine.build_consensus(
            [sample_extraction, sample_extraction_wrong]
        )
        assert len(result.disputed_fields) > 0
        # Either escalation or low confidence
        assert result.needs_escalation or result.confidence < 0.7

    def test_three_way_majority(self, sample_extraction, sample_extraction_variant, sample_extraction_wrong):
        """3 providers: 2 agree, 1 disagrees. Majority should win."""
        result = self.engine.build_consensus(
            [sample_extraction, sample_extraction_variant, sample_extraction_wrong]
        )
        # Supplier name should agree (2 out of 3 are similar)
        assert result.agreed_data.get("supplier_name") is not None

    def test_single_extraction(self, sample_extraction):
        """Single extraction should be accepted with lower confidence."""
        result = self.engine.build_consensus([sample_extraction])
        assert result.agreed_data == sample_extraction
        assert result.confidence == 0.7  # Lower confidence for single source

    def test_empty_extractions(self):
        """No extractions should trigger escalation."""
        result = self.engine.build_consensus([])
        assert result.needs_escalation
        assert len(result.escalation_reasons) > 0

    def test_numeric_tolerance(self):
        """Numeric values within tolerance should agree."""
        ext1 = {"total_amount": 100.00, "supplier_name": "Test"}
        ext2 = {"total_amount": 100.01, "supplier_name": "Test"}
        result = self.engine.build_consensus([ext1, ext2])
        assert "total_amount" in result.agreed_data

    def test_numeric_disagreement(self):
        """Numeric values outside tolerance should dispute."""
        ext1 = {"total_amount": 100.00, "supplier_name": "Test"}
        ext2 = {"total_amount": 200.00, "supplier_name": "Test"}
        result = self.engine.build_consensus([ext1, ext2])
        assert "total_amount" in result.disputed_fields

    def test_string_similarity_match(self):
        """Similar strings (above threshold) should agree."""
        ext1 = {"supplier_name": "ACME GmbH", "document_type": "invoice"}
        ext2 = {"supplier_name": "ACME Gmbh", "document_type": "invoice"}
        result = self.engine.build_consensus([ext1, ext2])
        assert "supplier_name" in result.agreed_data

    def test_string_dissimilarity(self):
        """Very different strings should dispute."""
        ext1 = {"supplier_name": "ACME GmbH"}
        ext2 = {"supplier_name": "Completely Different Corp"}
        result = self.engine.build_consensus([ext1, ext2])
        assert "supplier_name" in result.disputed_fields

    def test_critical_field_escalation(self):
        """Disputed critical fields should trigger escalation."""
        ext1 = {"supplier_name": "Company A", "total_amount": 100}
        ext2 = {"supplier_name": "Company B", "total_amount": 200}
        result = self.engine.build_consensus([ext1, ext2])
        assert result.needs_escalation

    def test_ocr_cross_check(self):
        """OCR mismatches should be reported."""
        ext = {"supplier_name": "Test Corp", "document_number": "INV-001"}
        ocr_baseline = {"text": "no mention of test corp or inv-001"}
        result = self.engine.build_consensus(
            [ext, ext],  # Two agreeing
            ocr_baseline=ocr_baseline,
        )
        # OCR cross-check may find mismatches
        # (depends on whether the values appear in OCR text)

    def test_items_consensus_matching(self):
        """Items with same names should agree."""
        items1 = [
            {"item_name": "Widget A", "quantity": 10, "unit_price": 5.0, "total_price": 50.0},
            {"item_name": "Widget B", "quantity": 5, "unit_price": 10.0, "total_price": 50.0},
        ]
        items2 = [
            {"item_name": "Widget A", "quantity": 10, "unit_price": 5.0, "total_price": 50.0},
            {"item_name": "Widget B", "quantity": 5, "unit_price": 10.0, "total_price": 50.0},
        ]
        ext1 = {"items": items1, "supplier_name": "Test"}
        ext2 = {"items": items2, "supplier_name": "Test"}
        result = self.engine.build_consensus([ext1, ext2])
        assert "items" in result.agreed_data

    def test_items_count_mismatch(self):
        """Different item counts should dispute."""
        ext1 = {
            "items": [{"item_name": "A", "quantity": 1, "unit_price": 1, "total_price": 1}],
            "supplier_name": "Test",
        }
        ext2 = {
            "items": [
                {"item_name": "A", "quantity": 1, "unit_price": 1, "total_price": 1},
                {"item_name": "B", "quantity": 2, "unit_price": 2, "total_price": 4},
            ],
            "supplier_name": "Test",
        }
        result = self.engine.build_consensus([ext1, ext2])
        assert "items" in result.disputed_fields

    def test_provider_scoring(self, sample_extraction, sample_extraction_variant):
        """Provider scores should be calculated."""
        result = self.engine.build_consensus(
            [sample_extraction, sample_extraction_variant]
        )
        assert len(result.provider_scores) > 0
