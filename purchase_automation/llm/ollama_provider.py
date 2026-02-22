"""Ollama LLM provider for local model inference.

Connects to a locally running Ollama instance via its REST API.
Supports multimodal models like llama3.2-vision, llava, bakllava.
"""

from __future__ import annotations

import base64
import json

import httpx

from purchase_automation.llm.base import LLMProvider, LLMResponse

DEFAULT_OLLAMA_URL = "http://localhost:11434"


class OllamaProvider(LLMProvider):
    """Ollama local LLM provider."""

    def __init__(
        self,
        *,
        model: str,
        api_url: str = DEFAULT_OLLAMA_URL,
    ):
        self._model = model
        self._api_url = api_url.rstrip("/")

    @property
    def provider_name(self) -> str:
        return "Ollama"

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
        # Ollama uses the /api/chat endpoint
        # Images are passed as base64-encoded strings in the 'images' field
        b64_images = [
            base64.standard_b64encode(img).decode("ascii") for img in images
        ]

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_prompt,
                "images": b64_images,
            },
        ]

        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {
                "num_predict": 4096,
            },
        }

        with httpx.Client(timeout=300) as client:
            resp = client.post(f"{self._api_url}/api/chat", json=payload)
            resp.raise_for_status()

        data = resp.json()
        raw_text = data.get("message", {}).get("content", "")
        raw_text = _strip_json_fences(raw_text)

        # Ollama provides token counts in eval_count / prompt_eval_count
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        return LLMResponse(
            raw_text=raw_text,
            provider_name=self.provider_name,
            model_name=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def health_check(self) -> bool:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{self._api_url}/api/tags")
                resp.raise_for_status()
                tags = resp.json()
                # Check if our model is available
                models = [m.get("name", "") for m in tags.get("models", [])]
                return any(self._model in m for m in models)
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
