"""Anthropic Claude LLM provider.

Supports Claude Sonnet, Haiku, and Opus models with native
multimodal (image) input.
"""

from __future__ import annotations

import base64
import json

import anthropic

from purchase_automation.llm.base import LLMProvider, LLMResponse


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, *, api_key: str, model: str, api_url: str | None = None):
        self._api_key = api_key
        self._model = model
        self._api_url = api_url
        self._client: anthropic.Anthropic | None = None

    @property
    def provider_name(self) -> str:
        return "Anthropic"

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            kwargs: dict = {"api_key": self._api_key}
            if self._api_url:
                kwargs["base_url"] = self._api_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _call_api(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: list[bytes],
        image_media_type: str = "image/png",
    ) -> LLMResponse:
        client = self._get_client()

        # Build multimodal content: images first, then text prompt
        content: list[dict] = []
        for img_bytes in images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_media_type,
                        "data": base64.standard_b64encode(img_bytes).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": user_prompt})

        response = client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )

        # Extract text from response
        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text += block.text

        # Strip markdown code fences if present
        raw_text = _strip_json_fences(raw_text)

        return LLMResponse(
            raw_text=raw_text,
            provider_name=self.provider_name,
            model_name=self._model,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
        )

    def health_check(self) -> bool:
        try:
            client = self._get_client()
            # Minimal API call to verify connectivity
            response = client.messages.create(
                model=self._model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Reply with OK"}],
            )
            return len(response.content) > 0
        except Exception:
            return False


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON output."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
