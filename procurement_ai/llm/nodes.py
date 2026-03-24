"""
LangGraph node implementations for the extraction pipeline.

Each node function takes the current state and returns a state update dict.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from .consensus import ConsensusEngine
from .local_trust import LocalLLMTrustPolicy
from .models import LLMProviderFactory
from .output_guard import OutputGuard
from .prompts import build_extraction_messages, build_vision_extraction_messages
from .sanitizer import InputSanitizer

logger = logging.getLogger(__name__)


def sanitize_input_node(state: dict) -> dict:
    """
    Node: Sanitize raw text input.

    Applies InputSanitizer to clean text before it reaches any LLM.
    Logs warnings about potential injection patterns.
    """
    raw_text = state.get("raw_text", "")
    sanitized_text, warnings = InputSanitizer.sanitize(raw_text)

    if warnings:
        logger.warning(
            f"Job {state.get('job_name')}: Sanitizer warnings: {warnings}"
        )

    return {
        "raw_text": sanitized_text,
        "escalation_reasons": warnings if warnings else [],
    }


def ocr_extraction_node(state: dict) -> dict:
    """
    Node: Run conventional OCR as a baseline.

    OCR results serve as an independent validation source
    for the LLM extraction results.
    """
    settings = state.get("settings", {})
    ocr_result = {"text": state.get("raw_text", ""), "source": "input_text"}

    # If we have images, run OCR on them for additional text
    images = state.get("document_images", [])
    if images:
        try:
            from ..extraction.ocr_engine import OCREngine
            from ..extraction.preprocessor import Preprocessor

            engine_name = settings.get("ocr_engine", "Tesseract")
            engine = OCREngine(engine_name=engine_name)

            ocr_texts = []
            for img_bytes in images[:5]:  # Limit to first 5 pages
                from PIL import Image
                import io

                img = Image.open(io.BytesIO(img_bytes))
                text = engine.extract(img)
                ocr_texts.append(text)

            if ocr_texts:
                ocr_result = {
                    "text": "\n\n".join(ocr_texts),
                    "source": f"ocr_{engine_name.lower()}",
                    "page_count": len(ocr_texts),
                }
        except Exception as e:
            logger.warning(f"OCR extraction failed: {e}")

    return {"ocr_result": ocr_result}


def classify_document_node(state: dict) -> dict:
    """
    Node: Pass through the user-supplied type hint or default to Auto-Detect.

    Document type is determined by the extraction LLMs (via consensus on
    the ``document_type`` field) rather than a separate classification call.
    A dedicated classifier was removed because it used a single LLM, was
    often wrong (e.g. labelling order confirmations as invoices), and its
    only purpose was providing a hint that could mislead extraction.
    """
    source_type_hint = state.get("source_type_hint")

    # If user already specified a type, pass it through as hint
    if source_type_hint and source_type_hint != "Auto-Detect":
        return {"source_type_hint": source_type_hint}

    return {"source_type_hint": "Auto-Detect"}


def llm_extraction_node_factory(provider: str) -> Callable:
    """
    Factory: Creates an extraction node for a specific LLM provider.

    Each node calls its LLM with the extraction prompt,
    validates the output through OutputGuard, and appends
    the result to the llm_results list.
    """

    def extraction_node(state: dict) -> dict:
        settings = state.get("settings", {})
        job_name = state.get("job_name", "unknown")

        llm = LLMProviderFactory.create(provider, settings)
        if not llm:
            logger.warning(f"Job {job_name}: Provider '{provider}' not available")
            return {"llm_results": []}

        is_local = provider == "local"
        type_hint = state.get("source_type_hint", "Auto-Detect")
        images = state.get("document_images", [])

        # Use vision for cloud providers when images are available
        use_vision = images and not is_local

        if use_vision:
            messages = build_vision_extraction_messages(
                sanitized_text=state.get("raw_text", ""),
                images=images,
                type_hint=type_hint,
            )
        else:
            messages = build_extraction_messages(
                sanitized_text=state.get("raw_text", ""),
                type_hint=type_hint,
                is_local=is_local,
            )

        if use_vision:
            logger.info(
                f"Job {job_name}: {provider} using vision with "
                f"{min(len(images), 5)} page image(s)"
            )

        start_time = time.time()
        try:
            # HumanMessage content is a string (text-only) or a list of
            # text + image_url blocks (vision). LangChain handles both.
            langchain_messages = [
                SystemMessage(content=messages[0]["content"]),
                HumanMessage(content=messages[1]["content"]),
            ]

            response = llm.invoke(langchain_messages)
            elapsed_ms = int((time.time() - start_time) * 1000)

            # Validate output
            extracted, errors = OutputGuard.validate_extraction(response.content)

            if errors:
                logger.info(
                    f"Job {job_name}: {provider} extraction warnings: {errors}"
                )

            if extracted is None:
                logger.error(
                    f"Job {job_name}: {provider} extraction failed validation: {errors}"
                )
                return {
                    "llm_results": [
                        {
                            "provider": provider,
                            "model_version": LLMProviderFactory.get_model_version(
                                provider, settings
                            ),
                            "extracted_data": None,
                            "confidence": 0.0,
                            "processing_time_ms": elapsed_ms,
                            "errors": errors,
                        }
                    ]
                }

            # Calculate token usage estimate
            token_count = _estimate_tokens(
                state.get("raw_text", ""), response.content
            )

            result = {
                "provider": provider,
                "model_version": LLMProviderFactory.get_model_version(
                    provider, settings
                ),
                "extracted_data": extracted.model_dump(mode="json"),
                "confidence": extracted.confidence_self_assessment,
                "processing_time_ms": elapsed_ms,
                "token_count": token_count,
                "errors": errors,
            }

            logger.info(
                f"Job {job_name}: {provider} extraction complete "
                f"({elapsed_ms}ms, confidence={extracted.confidence_self_assessment})"
            )
            return {"llm_results": [result]}

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.error(f"Job {job_name}: {provider} extraction error: {e}")
            return {
                "llm_results": [
                    {
                        "provider": provider,
                        "model_version": LLMProviderFactory.get_model_version(
                            provider, settings
                        ),
                        "extracted_data": None,
                        "confidence": 0.0,
                        "processing_time_ms": elapsed_ms,
                        "errors": [str(e)],
                    }
                ]
            }

    extraction_node.__name__ = f"llm_{provider}_extraction"
    return extraction_node


def build_consensus_node(state: dict) -> dict:
    """
    Node: Build consensus from all LLM extraction results.

    Compares results field-by-field, applies weighted voting
    (local LLMs may have reduced weight), and identifies disputes.
    """
    settings = state.get("settings", {})
    llm_results = state.get("llm_results", [])

    # Filter to successful extractions only
    valid_results = [
        r for r in llm_results if r.get("extracted_data") is not None
    ]

    if not valid_results:
        # Collect actual error messages from failed results
        error_details = []
        for r in llm_results:
            provider = r.get("provider", "unknown")
            errors = r.get("errors", [])
            if errors:
                error_details.append(f"{provider}: {'; '.join(errors)}")
        reason = "No valid extraction results from any LLM"
        if error_details:
            reason += "\n" + "\n".join(error_details)
        return {
            "consensus": None,
            "confidence": 0.0,
            "needs_escalation": True,
            "escalation_reasons": [reason],
        }

    # Build provider weights (local LLMs may have reduced weight)
    provider_weights = {}
    for r in valid_results:
        if r["provider"] == "local":
            provider_weights[r["provider"]] = LocalLLMTrustPolicy.get_weight(settings)
        else:
            provider_weights[r["provider"]] = 1.0

    # Run consensus engine
    extractions = [r["extracted_data"] for r in valid_results]
    ocr_baseline = state.get("ocr_result")

    engine = ConsensusEngine()
    consensus_result = engine.build_consensus(
        extractions=extractions,
        ocr_baseline=ocr_baseline,
        provider_weights=provider_weights,
    )

    return {
        "consensus": consensus_result.agreed_data,
        "confidence": consensus_result.confidence,
        "needs_escalation": consensus_result.needs_escalation,
        "escalation_reasons": consensus_result.escalation_reasons,
    }


def validation_node(state: dict) -> dict:
    """
    Node: Final validation of consensus results.

    Checks confidence threshold and applies additional validation rules.
    """
    settings = state.get("settings", {})
    confidence = state.get("confidence", 0.0)
    threshold = settings.get("confidence_threshold", 0.8)
    needs_escalation = state.get("needs_escalation", False)
    escalation_reasons = list(state.get("escalation_reasons", []))

    if confidence < threshold and not needs_escalation:
        needs_escalation = True
        escalation_reasons.append(
            f"Confidence {confidence:.1%} below threshold {threshold:.1%}"
        )

    consensus = state.get("consensus")
    if consensus:
        # Validate critical fields exist
        if not consensus.get("supplier_name"):
            needs_escalation = True
            escalation_reasons.append("Missing supplier_name in consensus")

        if not consensus.get("items"):
            needs_escalation = True
            escalation_reasons.append("No line items in consensus")

    return {
        "validated_data": consensus,
        "needs_escalation": needs_escalation,
        "escalation_reasons": escalation_reasons,
    }


def escalation_node(state: dict) -> dict:
    """
    Node: Handle escalation (log and notify).

    Creates an escalation log entry and sends notification email
    if configured.
    """
    job_name = state.get("job_name", "unknown")
    reasons = state.get("escalation_reasons", [])

    logger.warning(
        f"Job {job_name}: Escalation triggered. Reasons: {reasons}"
    )

    # Actual Frappe escalation log creation happens in the API layer
    # (this node runs in a LangGraph context, not directly in Frappe)
    return {
        "needs_escalation": True,
        "escalation_reasons": reasons,
    }


def _estimate_tokens(input_text: str, output_text: str) -> int:
    """Rough token count estimate (1 token ≈ 4 chars for English/German)."""
    return (len(input_text) + len(output_text)) // 4
