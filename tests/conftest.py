"""
Test fixtures and shared utilities.

Provides mock LLM responses, sample extraction data, and
Frappe test helpers.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


# ============================================================
# Sample extraction data
# ============================================================

SAMPLE_EXTRACTION = {
    "document_type": "invoice",
    "supplier_name": "ACME GmbH",
    "supplier_address": "Musterstr. 1, 12345 Berlin",
    "supplier_tax_id": "DE123456789",
    "supplier_email": "info@acme-gmbh.de",
    "supplier_phone": "+49 30 1234567",
    "document_number": "RE-2024-001",
    "document_date": "2024-01-15",
    "order_reference": "PO-2024-001",
    "delivery_date": "2024-01-20",
    "payment_terms": "30 Tage netto",
    "currency": "EUR",
    "items": [
        {
            "position": 1,
            "item_name": "Schrauben M8x50",
            "description": "Edelstahl A2",
            "quantity": 100,
            "uom": "Stk",
            "unit_price": 0.15,
            "total_price": 15.00,
        },
        {
            "position": 2,
            "item_name": "Muttern M8",
            "description": "Edelstahl A2",
            "quantity": 100,
            "uom": "Stk",
            "unit_price": 0.10,
            "total_price": 10.00,
        },
    ],
    "subtotal": 25.00,
    "tax_amount": 4.75,
    "total_amount": 29.75,
    "shipping_cost": 0.0,
    "notes": None,
    "confidence_self_assessment": 0.92,
}

SAMPLE_EXTRACTION_VARIANT = {
    **SAMPLE_EXTRACTION,
    "supplier_name": "ACME Gmbh",  # Slightly different capitalization
    "total_amount": 29.75,
    "confidence_self_assessment": 0.88,
}

SAMPLE_EXTRACTION_WRONG = {
    **SAMPLE_EXTRACTION,
    "supplier_name": "Completely Different Corp",
    "total_amount": 999.99,
    "confidence_self_assessment": 0.60,
}


@pytest.fixture
def sample_extraction():
    """Return a sample extraction dict."""
    return SAMPLE_EXTRACTION.copy()


@pytest.fixture
def sample_extraction_variant():
    """Return a slightly varied extraction dict."""
    return SAMPLE_EXTRACTION_VARIANT.copy()


@pytest.fixture
def sample_extraction_wrong():
    """Return a clearly wrong extraction dict."""
    return SAMPLE_EXTRACTION_WRONG.copy()


@pytest.fixture
def sample_extraction_json():
    """Return sample extraction as JSON string."""
    return json.dumps(SAMPLE_EXTRACTION)


@pytest.fixture
def mock_settings():
    """Return mock settings dict."""
    return {
        "enable_auto_processing": True,
        "development_mode": False,
        "default_company": "Test Company",
        "ocr_engine": "Tesseract",
        "confidence_threshold": 0.8,
        "min_llm_consensus": 2,
        "max_parallel_llms": 3,
        "auto_submit_documents": False,
        "escalation_email": "test@example.com",
        "claude_api_key": "test-claude-key",
        "openai_api_key": "test-openai-key",
        "gemini_api_key": None,
        "enable_local_llm": False,
        "local_llm_provider": "Ollama",
        "local_llm_base_url": "http://localhost:11434",
        "local_llm_model_name": "llama3.1:8b",
        "local_llm_api_key": None,
        "local_llm_context_length": 8192,
        "local_llm_gpu_layers": 0,
        "local_llm_timeout": 120,
        "local_llm_trust_level": "reduced",
    }


@pytest.fixture
def mock_llm_response():
    """Return a mock LangChain LLM response object."""
    response = MagicMock()
    response.content = json.dumps(SAMPLE_EXTRACTION)
    return response
