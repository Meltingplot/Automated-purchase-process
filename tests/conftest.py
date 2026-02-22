"""Test configuration and shared fixtures."""

import json
from datetime import date

import pytest

from purchase_automation.extraction.schemas import (
    DocumentType,
    ExtractedDocument,
    ExtractedLineItem,
)
from purchase_automation.llm.base import LLMProvider, LLMResponse


class MockLLMProvider(LLMProvider):
    """Mock LLM provider for testing.

    Returns a pre-configured JSON response regardless of input.
    """

    def __init__(
        self,
        *,
        name: str = "MockProvider",
        model: str = "mock-model",
        response_data: dict | None = None,
        error: str | None = None,
    ):
        self._name = name
        self._model = model
        self._response_data = response_data
        self._error = error

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def model_name(self) -> str:
        return self._model

    def _call_api(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[bytes],
        image_media_type: str = "image/png",
    ) -> LLMResponse:
        if self._error:
            return LLMResponse(
                raw_text="",
                provider_name=self._name,
                model_name=self._model,
                error=self._error,
            )
        return LLMResponse(
            raw_text=json.dumps(self._response_data or {}),
            provider_name=self._name,
            model_name=self._model,
            prompt_tokens=100,
            completion_tokens=200,
        )

    def health_check(self) -> bool:
        return self._error is None


@pytest.fixture
def sample_invoice_data() -> dict:
    """Sample extracted invoice data."""
    return {
        "document_type": "purchase_invoice",
        "supplier_name": "Muster GmbH",
        "supplier_address": "Musterstr. 1, 12345 Musterstadt",
        "supplier_tax_id": "DE123456789",
        "document_number": "RE-2026-001",
        "document_date": "2026-02-15",
        "delivery_date": None,
        "due_date": "2026-03-15",
        "line_items": [
            {
                "position": 1,
                "item_description": "Widget A",
                "item_code_supplier": "WA-100",
                "quantity": 10,
                "unit": "Stk",
                "unit_price": 25.00,
                "total_price": 250.00,
                "tax_rate": 19.0,
            },
            {
                "position": 2,
                "item_description": "Widget B",
                "item_code_supplier": "WB-200",
                "quantity": 5,
                "unit": "Stk",
                "unit_price": 50.00,
                "total_price": 250.00,
                "tax_rate": 19.0,
            },
        ],
        "subtotal": 500.00,
        "tax_amount": 95.00,
        "total_amount": 595.00,
        "currency": "EUR",
        "notes": None,
    }


@pytest.fixture
def sample_invoice_data_variant(sample_invoice_data) -> dict:
    """Slightly different extraction — same document, minor differences."""
    data = sample_invoice_data.copy()
    data["line_items"] = [item.copy() for item in data["line_items"]]
    # Minor supplier name variation
    data["supplier_name"] = "Muster GmbH "  # trailing space
    return data


@pytest.fixture
def sample_invoice_data_conflict(sample_invoice_data) -> dict:
    """Conflicting extraction — same document, major differences."""
    data = sample_invoice_data.copy()
    data["line_items"] = [item.copy() for item in data["line_items"]]
    # Wrong total
    data["total_amount"] = 999.99
    # Wrong quantity
    data["line_items"][0]["quantity"] = 20  # was 10
    return data


@pytest.fixture
def mock_provider_a(sample_invoice_data) -> MockLLMProvider:
    return MockLLMProvider(
        name="ProviderA",
        model="model-a",
        response_data=sample_invoice_data,
    )


@pytest.fixture
def mock_provider_b(sample_invoice_data) -> MockLLMProvider:
    return MockLLMProvider(
        name="ProviderB",
        model="model-b",
        response_data=sample_invoice_data,
    )
