"""Tests for LLM provider abstraction."""

import json

import pytest

from purchase_automation.llm.base import LLMResponse
from purchase_automation.llm.registry import create_provider, PROVIDERS
from tests.conftest import MockLLMProvider


class TestMockProvider:
    def test_successful_extraction(self, sample_invoice_data):
        provider = MockLLMProvider(
            response_data=sample_invoice_data,
        )
        response = provider.extract(
            system_prompt="test",
            user_prompt="test",
            images=[b"fake_image"],
        )
        assert response.success
        data = json.loads(response.raw_text)
        assert data["supplier_name"] == "Muster GmbH"

    def test_error_response(self):
        provider = MockLLMProvider(error="API connection failed")
        response = provider.extract(
            system_prompt="test",
            user_prompt="test",
            images=[b"fake_image"],
        )
        assert not response.success
        assert "API connection failed" in response.error

    def test_health_check_healthy(self, sample_invoice_data):
        provider = MockLLMProvider(response_data=sample_invoice_data)
        assert provider.health_check() is True

    def test_health_check_unhealthy(self):
        provider = MockLLMProvider(error="down")
        assert provider.health_check() is False

    def test_duration_tracked(self, sample_invoice_data):
        provider = MockLLMProvider(response_data=sample_invoice_data)
        response = provider.extract(
            system_prompt="test",
            user_prompt="test",
            images=[b"fake_image"],
        )
        assert response.duration_seconds >= 0


class TestProviderRegistry:
    def test_all_providers_registered(self):
        assert "Anthropic" in PROVIDERS
        assert "OpenAI" in PROVIDERS
        assert "Ollama" in PROVIDERS
        assert "OpenAI-Compatible" in PROVIDERS

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("NonExistent", model="test")


class TestLLMResponse:
    def test_success_response(self):
        resp = LLMResponse(
            raw_text='{"key": "value"}',
            provider_name="Test",
            model_name="test-model",
        )
        assert resp.success is True

    def test_error_response(self):
        resp = LLMResponse(
            raw_text="",
            provider_name="Test",
            model_name="test-model",
            error="Connection refused",
        )
        assert resp.success is False

    def test_empty_text_not_success(self):
        resp = LLMResponse(
            raw_text="",
            provider_name="Test",
            model_name="test-model",
        )
        assert resp.success is False
