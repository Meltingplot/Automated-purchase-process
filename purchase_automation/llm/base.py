"""Abstract base class for LLM providers.

All LLM providers (cloud and local) implement this interface.
The provider is responsible ONLY for sending images + prompt to the model
and returning the raw JSON response. It has NO access to ERPNext or
any other system — this is a deliberate security boundary.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""

    raw_text: str
    provider_name: str
    model_name: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.error is None and len(self.raw_text) > 0


class LLMProvider(ABC):
    """Abstract base for all LLM providers.

    Security: The provider interface is intentionally minimal.
    - It accepts images (bytes) and a text prompt.
    - It returns raw text (expected to be JSON).
    - It has NO tools, NO function calling, NO system access.
    - The caller is responsible for parsing and validating the output.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name (e.g. 'Anthropic', 'Ollama')."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier (e.g. 'claude-sonnet-4-20250514', 'llama3.2-vision')."""
        ...

    @abstractmethod
    def _call_api(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[bytes],
        image_media_type: str = "image/png",
    ) -> LLMResponse:
        """Make the actual API call. Implemented by each provider."""
        ...

    def extract(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[bytes],
        image_media_type: str = "image/png",
    ) -> LLMResponse:
        """Extract data from document images using this LLM.

        This is the public entry point. It wraps _call_api with
        timing and error handling.
        """
        start = time.monotonic()
        try:
            response = self._call_api(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                images=images,
                image_media_type=image_media_type,
            )
            response.duration_seconds = time.monotonic() - start
            return response
        except Exception as e:
            return LLMResponse(
                raw_text="",
                provider_name=self.provider_name,
                model_name=self.model_name,
                duration_seconds=time.monotonic() - start,
                error=f"{type(e).__name__}: {e}",
            )

    @abstractmethod
    def health_check(self) -> bool:
        """Check if the provider is reachable and operational."""
        ...
