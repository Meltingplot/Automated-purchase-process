"""
Trust-level system for local LLMs.

Local LLMs (7B-70B parameters) are typically more susceptible to
prompt injection than large cloud models. The trust-level system
controls how strongly a local LLM's vote counts in consensus.

Trust Levels:
- "full":            Equal vote weight with cloud LLMs (recommended for 70B+)
- "reduced":         Half weight in consensus (recommended for 13B-70B)
- "validation_only": No vote, used only for cross-checking (recommended for <13B)
"""

from __future__ import annotations


class LocalLLMTrustPolicy:
    """Manages trust weights for local LLMs in the consensus process."""

    TRUST_WEIGHTS: dict[str, float] = {
        "full": 1.0,
        "reduced": 0.5,
        "validation_only": 0.0,
    }

    # Automatic trust-level recommendations based on model size
    MODEL_SIZE_HEURISTICS: dict[str, str] = {
        "llama3.1:70b": "full",
        "llama3.1:8b": "reduced",
        "llama3.2": "reduced",
        "mistral-nemo": "reduced",
        "mixtral:8x7b": "full",
        "qwen2.5:72b": "full",
        "qwen2.5:14b": "reduced",
        "qwen2.5:7b": "validation_only",
        "phi-3:14b": "reduced",
        "gemma2:27b": "full",
        "gemma2:9b": "reduced",
        "deepseek-r1:70b": "full",
        "deepseek-r1:14b": "reduced",
        "command-r:35b": "full",
    }

    @classmethod
    def get_weight(cls, settings: dict) -> float:
        """Get the consensus vote weight for the local LLM."""
        trust = settings.get("local_llm_trust_level", "reduced")
        return cls.TRUST_WEIGHTS.get(trust, 0.5)

    @classmethod
    def suggest_trust_level(cls, model_name: str) -> str:
        """Suggest a trust level based on model name pattern matching."""
        model_lower = model_name.lower()
        for pattern, level in cls.MODEL_SIZE_HEURISTICS.items():
            if pattern in model_lower:
                return level
        return "reduced"  # Conservative default
