"""LLM Provider registry and factory.

Creates provider instances based on configuration. In a Frappe context,
configuration is read from the 'Purchase Automation Settings' DocType.
For standalone use (e.g. tests), providers can be created directly.

Provider modules are imported lazily to avoid requiring all SDKs
(anthropic, openai, httpx) to be installed at once.
"""

from __future__ import annotations

from purchase_automation.llm.base import LLMProvider

# Provider types available for creation
PROVIDERS: dict[str, str] = {
    "Anthropic": "purchase_automation.llm.anthropic_provider.AnthropicProvider",
    "OpenAI": "purchase_automation.llm.openai_provider.OpenAIProvider",
    "OpenAI-Compatible": "purchase_automation.llm.openai_provider.OpenAIProvider",
    "Ollama": "purchase_automation.llm.ollama_provider.OllamaProvider",
}


def _import_class(dotted_path: str) -> type:
    """Import a class from a dotted module path."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def create_provider(
    provider_type: str,
    *,
    model: str,
    api_key: str = "",
    api_url: str = "",
) -> LLMProvider:
    """Create an LLM provider instance.

    Args:
        provider_type: One of 'Anthropic', 'OpenAI', 'OpenAI-Compatible', 'Ollama'.
        model: Model identifier (e.g. 'claude-sonnet-4-20250514').
        api_key: API key (not needed for Ollama).
        api_url: Custom API URL (required for Ollama and OpenAI-Compatible).
    """
    if provider_type not in PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider_type}'. "
            f"Available: {', '.join(PROVIDERS.keys())}"
        )

    cls = _import_class(PROVIDERS[provider_type])

    if provider_type == "Ollama":
        return cls(
            model=model,
            api_url=api_url or "http://localhost:11434",
        )

    # Anthropic, OpenAI, OpenAI-Compatible all take api_key + model
    return cls(
        api_key=api_key,
        model=model,
        api_url=api_url or None,
    )


def get_provider(role: str = "primary") -> LLMProvider:
    """Create an LLM provider from Frappe settings.

    Args:
        role: 'primary' or 'secondary' — selects which model config to use.

    Returns:
        Configured LLMProvider instance.

    Raises:
        ValueError: If role is invalid or settings are incomplete.
    """
    try:
        import frappe
    except ImportError:
        raise RuntimeError(
            "Frappe not available. Use create_provider() for standalone usage."
        )

    settings = frappe.get_doc("Purchase Automation Settings")

    if role == "primary":
        provider_type = settings.primary_llm_provider
        model = settings.primary_model
        api_key = settings.get_password("primary_api_key") if settings.primary_api_key else ""
        api_url = settings.primary_api_url or ""
    elif role == "secondary":
        provider_type = settings.secondary_llm_provider
        model = settings.secondary_model
        api_key = settings.get_password("secondary_api_key") if settings.secondary_api_key else ""
        api_url = settings.secondary_api_url or ""
    else:
        raise ValueError(f"Invalid role '{role}'. Must be 'primary' or 'secondary'.")

    if not provider_type or not model:
        raise ValueError(
            f"LLM provider configuration for '{role}' is incomplete. "
            f"Please configure in Purchase Automation Settings."
        )

    return create_provider(
        provider_type=provider_type,
        model=model,
        api_key=api_key,
        api_url=api_url,
    )
