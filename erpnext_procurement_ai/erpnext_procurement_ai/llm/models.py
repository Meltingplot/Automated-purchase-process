"""
LLM Provider Factory with unified interface for all providers.

Supports:
- Cloud: Claude (Anthropic), GPT-4 (OpenAI), Gemini (Google)
- Local: Ollama, vLLM, llama.cpp, LM Studio (all via OpenAI-compatible API)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class LLMProviderFactory:
    """
    Creates LangChain-compatible chat models for all providers.

    All local LLM providers (Ollama, vLLM, llama.cpp, LM Studio)
    implement the OpenAI API spec, so we use ChatOpenAI with a
    custom base_url for all of them.
    """

    # Endpoint suffix mapping for local providers
    ENDPOINT_MAP = {
        "Ollama": "/v1",
        "vLLM": "",
        "llama.cpp": "",
        "LM Studio": "/v1",
        "Custom": "",
    }

    @staticmethod
    def create(provider: str, settings: dict):
        """
        Factory method: create the right LLM based on provider name.

        Args:
            provider: "claude", "openai", "gemini", or "local"
            settings: dict from AIProcurementSettings.get_settings_dict()

        Returns:
            LangChain BaseChatModel or None if provider is not configured
        """
        if provider == "claude":
            return LLMProviderFactory._create_claude(settings)
        elif provider == "openai":
            return LLMProviderFactory._create_openai(settings)
        elif provider == "gemini":
            return LLMProviderFactory._create_gemini(settings)
        elif provider == "local":
            return LLMProviderFactory._create_local(settings)
        else:
            raise ValueError(f"Unknown LLM provider: {provider}")

    @staticmethod
    def _create_claude(settings: dict):
        api_key = settings.get("claude_api_key")
        if not api_key:
            return None

        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model="claude-sonnet-4-5-20250929",
            api_key=api_key,
            max_tokens=4096,
            timeout=60,
        )

    @staticmethod
    def _create_openai(settings: dict):
        api_key = settings.get("openai_api_key")
        if not api_key:
            return None

        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model="gpt-4o",
            api_key=api_key,
            max_tokens=4096,
            timeout=60,
        )

    @staticmethod
    def _create_gemini(settings: dict):
        api_key = settings.get("gemini_api_key")
        if not api_key:
            return None

        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            google_api_key=api_key,
            max_output_tokens=4096,
        )

    @staticmethod
    def _create_local(settings: dict):
        """
        Create local LLM via OpenAI-compatible API.

        All supported local servers (Ollama, vLLM, llama.cpp, LM Studio)
        implement the OpenAI chat completions API.
        """
        if not settings.get("enable_local_llm"):
            return None

        from langchain_openai import ChatOpenAI

        local_provider = settings.get("local_llm_provider", "Ollama")
        base_url = settings.get("local_llm_base_url", "http://localhost:11434")
        model_name = settings.get("local_llm_model_name", "llama3.1:8b")
        timeout = settings.get("local_llm_timeout", 120)
        api_key = settings.get("local_llm_api_key") or "not-needed"

        suffix = LLMProviderFactory.ENDPOINT_MAP.get(local_provider, "")
        openai_base = f"{base_url.rstrip('/')}{suffix}"

        context_length = settings.get("local_llm_context_length", 8192) or 8192

        return ChatOpenAI(
            model=model_name,
            base_url=openai_base,
            api_key=api_key,
            max_tokens=min(4096, context_length // 2),
            timeout=timeout,
            temperature=0.0,
        )

    @staticmethod
    def get_active_providers(settings: dict) -> list[str]:
        """Return list of configured/active provider names."""
        providers = []
        if settings.get("claude_api_key"):
            providers.append("claude")
        if settings.get("openai_api_key"):
            providers.append("openai")
        if settings.get("gemini_api_key"):
            providers.append("gemini")
        if settings.get("enable_local_llm") and settings.get("local_llm_base_url"):
            providers.append("local")
        return providers

    @staticmethod
    def get_model_version(provider: str, settings: dict) -> str:
        """Return the model version string for a given provider."""
        if provider == "claude":
            return "claude-sonnet-4-5-20250929"
        elif provider == "openai":
            return "gpt-4o"
        elif provider == "gemini":
            return "gemini-1.5-flash"
        elif provider == "local":
            return settings.get("local_llm_model_name", "unknown-local")
        return "unknown"
