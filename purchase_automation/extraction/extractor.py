"""Dual-model document extraction pipeline.

Orchestrates the full extraction flow:
1. Preprocess document (PDF → images)
2. Run extraction on two independent LLM models
3. Parse and validate both outputs
4. Compare results and determine escalation level

Security: The extractor NEVER passes LLM output to any system.
It only returns structured, validated data for the orchestrator
to act upon deterministically.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from pydantic import ValidationError

from purchase_automation.extraction.comparator import (
    ComparisonLevel,
    ComparisonResult,
    compare_extractions,
)
from purchase_automation.extraction.preprocessor import prepare_document
from purchase_automation.extraction.prompt_templates import (
    get_system_prompt,
    get_user_prompt,
)
from purchase_automation.extraction.schemas import ExtractedDocument
from purchase_automation.llm.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Complete result of a dual-model extraction."""

    # Individual model results
    response_a: LLMResponse
    response_b: LLMResponse

    # Parsed results (None if parsing failed)
    parsed_a: ExtractedDocument | None
    parsed_b: ExtractedDocument | None

    # Parsing errors
    parse_error_a: str | None
    parse_error_b: str | None

    # Comparison (None if either parse failed)
    comparison: ComparisonResult | None

    @property
    def success(self) -> bool:
        """Both models produced valid, comparable results."""
        return (
            self.parsed_a is not None
            and self.parsed_b is not None
            and self.comparison is not None
        )

    @property
    def level(self) -> ComparisonLevel | None:
        """Escalation level from comparison."""
        if self.comparison:
            return self.comparison.level
        return None

    @property
    def merged_result(self) -> dict | None:
        """Merged extraction result (only if auto-accepted)."""
        if self.comparison:
            return self.comparison.merged_result
        return None

    @property
    def best_result(self) -> ExtractedDocument | None:
        """Return the best available result.

        For auto-accept: primary model's result.
        For review/reject: primary model's result (as starting point).
        If only one model succeeded: that model's result.
        """
        if self.parsed_a is not None:
            return self.parsed_a
        return self.parsed_b

    def to_dict(self) -> dict:
        """Serialize for storage in Frappe JSON field."""
        return {
            "model_a": {
                "provider": self.response_a.provider_name,
                "model": self.response_a.model_name,
                "prompt_tokens": self.response_a.prompt_tokens,
                "completion_tokens": self.response_a.completion_tokens,
                "duration_seconds": round(self.response_a.duration_seconds, 2),
                "error": self.response_a.error,
                "parse_error": self.parse_error_a,
            },
            "model_b": {
                "provider": self.response_b.provider_name,
                "model": self.response_b.model_name,
                "prompt_tokens": self.response_b.prompt_tokens,
                "completion_tokens": self.response_b.completion_tokens,
                "duration_seconds": round(self.response_b.duration_seconds, 2),
                "error": self.response_b.error,
                "parse_error": self.parse_error_b,
            },
            "comparison": self.comparison.to_dict() if self.comparison else None,
        }


def extract_document(
    *,
    file_bytes: bytes,
    filename: str,
    provider_a: LLMProvider,
    provider_b: LLMProvider,
    auto_accept_threshold: float = 0.95,
    review_threshold: float = 0.70,
) -> ExtractionResult:
    """Run dual-model extraction on a document.

    Args:
        file_bytes: Raw document file content.
        filename: Original filename (for type detection).
        provider_a: Primary LLM provider.
        provider_b: Secondary LLM provider.
        auto_accept_threshold: Score for auto-accept.
        review_threshold: Score below which = reject.

    Returns:
        ExtractionResult with both model outputs and comparison.
    """
    # Step 1: Preprocess
    logger.info("Preprocessing document: %s", filename)
    images, media_type = prepare_document(file_bytes, filename)
    logger.info("Produced %d image(s) from document", len(images))

    system_prompt = get_system_prompt()
    user_prompt = get_user_prompt()

    # Step 2: Run both models
    # NOTE: In a Frappe background job context, these run sequentially.
    # For true parallelism, use separate background jobs per model.
    logger.info(
        "Running extraction with Model A: %s/%s",
        provider_a.provider_name,
        provider_a.model_name,
    )
    response_a = provider_a.extract(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        images=images,
        image_media_type=media_type,
    )

    logger.info(
        "Running extraction with Model B: %s/%s",
        provider_b.provider_name,
        provider_b.model_name,
    )
    response_b = provider_b.extract(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        images=images,
        image_media_type=media_type,
    )

    # Step 3: Parse and validate both results
    parsed_a, error_a = _parse_response(response_a)
    parsed_b, error_b = _parse_response(response_b)

    # Step 4: Compare if both parsed successfully
    comparison = None
    if parsed_a is not None and parsed_b is not None:
        comparison = compare_extractions(
            parsed_a,
            parsed_b,
            auto_accept_threshold=auto_accept_threshold,
            review_threshold=review_threshold,
        )
        logger.info(
            "Comparison result: level=%s, score=%.4f",
            comparison.level.value,
            comparison.overall_score,
        )
    else:
        logger.warning(
            "Cannot compare: parse_error_a=%s, parse_error_b=%s",
            error_a,
            error_b,
        )

    return ExtractionResult(
        response_a=response_a,
        response_b=response_b,
        parsed_a=parsed_a,
        parsed_b=parsed_b,
        parse_error_a=error_a,
        parse_error_b=error_b,
        comparison=comparison,
    )


def _parse_response(
    response: LLMResponse,
) -> tuple[ExtractedDocument | None, str | None]:
    """Parse and validate an LLM response into an ExtractedDocument.

    Returns (parsed_doc, error_message).
    """
    if not response.success:
        return None, f"LLM error: {response.error}"

    try:
        data = json.loads(response.raw_text)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON: {e}"

    try:
        doc = ExtractedDocument.model_validate(data)
        return doc, None
    except ValidationError as e:
        return None, f"Schema validation failed: {e}"
