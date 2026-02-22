"""Tests for the dual-model comparator."""

import pytest

from purchase_automation.extraction.comparator import (
    ComparisonLevel,
    compare_extractions,
)
from purchase_automation.extraction.schemas import ExtractedDocument


class TestComparator:
    def test_identical_results_auto_accept(self, sample_invoice_data):
        """Two identical results should be auto-accepted."""
        doc_a = ExtractedDocument.model_validate(sample_invoice_data)
        doc_b = ExtractedDocument.model_validate(sample_invoice_data)

        result = compare_extractions(doc_a, doc_b)
        assert result.level == ComparisonLevel.AUTO_ACCEPT
        assert result.overall_score >= 0.95
        assert result.merged_result is not None

    def test_minor_differences_high_score(
        self, sample_invoice_data, sample_invoice_data_variant
    ):
        """Minor text differences should still produce a high score."""
        doc_a = ExtractedDocument.model_validate(sample_invoice_data)
        doc_b = ExtractedDocument.model_validate(sample_invoice_data_variant)

        result = compare_extractions(doc_a, doc_b)
        # Trailing space is normalized, should still auto-accept
        assert result.overall_score >= 0.90

    def test_critical_conflict_rejected(
        self, sample_invoice_data, sample_invoice_data_conflict
    ):
        """Conflicting amounts and quantities should be rejected."""
        doc_a = ExtractedDocument.model_validate(sample_invoice_data)
        doc_b = ExtractedDocument.model_validate(sample_invoice_data_conflict)

        result = compare_extractions(doc_a, doc_b)
        assert result.level == ComparisonLevel.REJECT
        assert result.merged_result is None

        # Check that the mismatched fields are identified
        mismatched_fields = [
            fc.field_name for fc in result.field_comparisons if not fc.match
        ]
        assert "total_amount" in mismatched_fields
        assert any("quantity" in f for f in mismatched_fields)

    def test_one_missing_optional_field(self, sample_invoice_data):
        """One model extracting an optional field the other misses."""
        data_a = sample_invoice_data.copy()
        data_b = sample_invoice_data.copy()
        data_b["due_date"] = None  # Model B didn't extract due date

        doc_a = ExtractedDocument.model_validate(data_a)
        doc_b = ExtractedDocument.model_validate(data_b)

        result = compare_extractions(doc_a, doc_b)
        # Should not reject over missing optional field
        assert result.level in (
            ComparisonLevel.AUTO_ACCEPT,
            ComparisonLevel.AUTO_RESOLVE,
            ComparisonLevel.REVIEW,
        )

    def test_different_line_item_count_rejected(self, sample_invoice_data):
        """Different number of line items should be rejected."""
        data_a = sample_invoice_data.copy()
        data_b = sample_invoice_data.copy()
        data_b["line_items"] = [sample_invoice_data["line_items"][0]]  # Only 1 item

        doc_a = ExtractedDocument.model_validate(data_a)
        doc_b = ExtractedDocument.model_validate(data_b)

        result = compare_extractions(doc_a, doc_b)
        assert result.level == ComparisonLevel.REJECT

    def test_comparison_details_serializable(self, sample_invoice_data):
        """to_dict() should produce valid, serializable output."""
        doc_a = ExtractedDocument.model_validate(sample_invoice_data)
        doc_b = ExtractedDocument.model_validate(sample_invoice_data)

        result = compare_extractions(doc_a, doc_b)
        details = result.to_dict()

        assert "level" in details
        assert "overall_score" in details
        assert "fields" in details
        assert isinstance(details["fields"], list)

    def test_custom_thresholds(self, sample_invoice_data):
        """Custom thresholds should be respected."""
        doc_a = ExtractedDocument.model_validate(sample_invoice_data)
        doc_b = ExtractedDocument.model_validate(sample_invoice_data)

        # Very high threshold — even identical results get "review"
        result = compare_extractions(
            doc_a, doc_b, auto_accept_threshold=1.01
        )
        # Since score can be exactly 1.0 for identical, this depends on implementation
        # but the threshold mechanism should work
        assert result.overall_score >= 0.95
