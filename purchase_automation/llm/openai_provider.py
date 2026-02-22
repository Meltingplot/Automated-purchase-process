"""OpenAI and OpenAI-compatible LLM provider.

Supports OpenAI GPT-4o and any OpenAI-compatible API endpoint
(vLLM, LocalAI, LM Studio, llama.cpp server, etc.).
"""

from __future__ import annotations

import base64

import openai

from purchase_automation.llm.base import LLMProvider, LLMResponse


class OpenAIProvider(LLMProvider):
    """OpenAI / OpenAI-compatible API provider."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        api_url: str | None = None,
    ):
        self._api_key = api_key
        self._model = model
        self._api_url = api_url
        self._client: openai.OpenAI | None = None

    @property
    def provider_name(self) -> str:
        if self._api_url and "openai.com" not in self._api_url:
            return "OpenAI-Compatible"
        return "OpenAI"

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self) -> openai.OpenAI:
        if self._client is None:
            kwargs: dict = {"api_key": self._api_key}
            if self._api_url:
                kwargs["base_url"] = self._api_url
            self._client = openai.OpenAI(**kwargs)
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

        # Build multimodal content
        content: list[dict] = []
        for img_bytes in images:
            b64 = base64.standard_b64encode(img_bytes).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image_media_type};base64,{b64}",
                    },
                }
            )
        content.append({"type": "text", "text": user_prompt})

        response = client.chat.completions.create(
            model=self._model,
            max_tokens=4096,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
        )

        raw_text = response.choices[0].message.content or ""
        raw_text = _strip_json_fences(raw_text)

        prompt_tokens = 0
        completion_tokens = 0
        if response.usage:
            prompt_tokens = response.usage.prompt_tokens
            completion_tokens = response.usage.completion_tokens

        return LLMResponse(
            raw_text=raw_text,
            provider_name=self.provider_name,
            model_name=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def health_check(self) -> bool:
        try:
            client = self._get_client()
            response = client.chat.completions.create(
                model=self._model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Reply with OK"}],
            )
            return len(response.choices) > 0
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
