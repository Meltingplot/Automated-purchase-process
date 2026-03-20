"""
Health check for local LLM servers.

Verifies connectivity and model availability for Ollama, vLLM,
llama.cpp, and LM Studio before starting extraction jobs.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class LocalLLMHealthCheck:
    """
    Checks whether the local LLM server is reachable and the model is loaded.

    Called during:
    - Settings validation (when saving AI Procurement Settings)
    - Before each extraction job starts
    """

    @staticmethod
    def check(settings: dict) -> dict:
        """
        Run health check against the configured local LLM.

        Returns:
            {
                "healthy": bool,
                "model_loaded": bool,
                "model_name": str | None,
                "error": str | None,
            }
        """
        base_url = settings.get("local_llm_base_url", "http://localhost:11434")
        provider = settings.get("local_llm_provider", "Ollama")

        try:
            if provider == "Ollama":
                return LocalLLMHealthCheck._check_ollama(base_url, settings)
            else:
                return LocalLLMHealthCheck._check_openai_compatible(
                    base_url, provider, settings
                )
        except requests.ConnectionError:
            return {
                "healthy": False,
                "model_loaded": False,
                "model_name": None,
                "error": (
                    f"Cannot connect to {base_url}. "
                    f"Is the {provider} server running?"
                ),
            }
        except Exception as e:
            return {
                "healthy": False,
                "model_loaded": False,
                "model_name": None,
                "error": str(e),
            }

    @staticmethod
    def _check_ollama(base_url: str, settings: dict) -> dict:
        """Ollama-specific health check via /api/tags."""
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        resp.raise_for_status()

        models = resp.json().get("models", [])
        target = settings.get("local_llm_model_name", "")
        loaded = any(m.get("name") == target for m in models)

        if not loaded:
            available = [m.get("name") for m in models]
            return {
                "healthy": True,
                "model_loaded": False,
                "model_name": target,
                "error": (
                    f"Model '{target}' not loaded. "
                    f"Available: {available}. "
                    f"Run 'ollama pull {target}' to download."
                ),
            }

        return {
            "healthy": True,
            "model_loaded": True,
            "model_name": target,
            "error": None,
        }

    @staticmethod
    def _check_openai_compatible(
        base_url: str, provider: str, settings: dict
    ) -> dict:
        """Health check for OpenAI-compatible servers (vLLM, llama.cpp, LM Studio)."""
        resp = requests.get(f"{base_url}/v1/models", timeout=5)
        resp.raise_for_status()

        models = resp.json().get("data", [])
        model_name = models[0]["id"] if models else None

        return {
            "healthy": True,
            "model_loaded": len(models) > 0,
            "model_name": model_name,
            "error": None if models else f"No models loaded on {provider} server",
        }
