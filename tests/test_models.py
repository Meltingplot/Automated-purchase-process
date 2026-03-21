"""Tests for LLM Provider Factory."""

from __future__ import annotations

import pytest

from procurement_ai.llm.local_trust import LocalLLMTrustPolicy
from procurement_ai.llm.models import LLMProviderFactory


class TestLLMProviderFactory:
    """Test LLM provider factory."""

    def test_get_active_providers_all(self, mock_settings):
        mock_settings["gemini_api_key"] = "test-gemini-key"
        mock_settings["enable_local_llm"] = True
        providers = LLMProviderFactory.get_active_providers(mock_settings)
        assert "claude" in providers
        assert "openai" in providers
        assert "gemini" in providers
        assert "local" in providers

    def test_get_active_providers_none(self):
        settings = {
            "claude_api_key": None,
            "openai_api_key": None,
            "gemini_api_key": None,
            "enable_local_llm": False,
            "local_llm_base_url": None,
        }
        providers = LLMProviderFactory.get_active_providers(settings)
        assert len(providers) == 0

    def test_get_active_providers_partial(self, mock_settings):
        providers = LLMProviderFactory.get_active_providers(mock_settings)
        assert "claude" in providers
        assert "openai" in providers
        assert "gemini" not in providers
        assert "local" not in providers

    def test_get_model_version_claude(self, mock_settings):
        version = LLMProviderFactory.get_model_version("claude", mock_settings)
        assert "claude" in version

    def test_get_model_version_local(self, mock_settings):
        version = LLMProviderFactory.get_model_version("local", mock_settings)
        assert version == "llama3.1:8b"

    def test_create_unknown_provider(self, mock_settings):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            LLMProviderFactory.create("unknown", mock_settings)

    def test_create_claude_no_key(self):
        result = LLMProviderFactory.create("claude", {"claude_api_key": None})
        assert result is None

    def test_create_local_disabled(self):
        result = LLMProviderFactory.create("local", {"enable_local_llm": False})
        assert result is None


class TestLocalLLMTrustPolicy:
    """Test trust-level system for local LLMs."""

    def test_full_weight(self):
        settings = {"local_llm_trust_level": "full"}
        assert LocalLLMTrustPolicy.get_weight(settings) == 1.0

    def test_reduced_weight(self):
        settings = {"local_llm_trust_level": "reduced"}
        assert LocalLLMTrustPolicy.get_weight(settings) == 0.5

    def test_validation_only_weight(self):
        settings = {"local_llm_trust_level": "validation_only"}
        assert LocalLLMTrustPolicy.get_weight(settings) == 0.0

    def test_default_weight(self):
        settings = {}
        assert LocalLLMTrustPolicy.get_weight(settings) == 0.5

    def test_suggest_70b(self):
        assert LocalLLMTrustPolicy.suggest_trust_level("llama3.1:70b") == "full"

    def test_suggest_8b(self):
        assert LocalLLMTrustPolicy.suggest_trust_level("llama3.1:8b") == "reduced"

    def test_suggest_7b(self):
        assert LocalLLMTrustPolicy.suggest_trust_level("qwen2.5:7b") == "validation_only"

    def test_suggest_unknown(self):
        assert LocalLLMTrustPolicy.suggest_trust_level("unknown-model") == "reduced"
