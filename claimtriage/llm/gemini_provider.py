"""Google Generative Language API adapter.

Kept as a second real provider so that "place the model behind an interface you
control so the provider can be changed by configuration" is a demonstrated
property rather than an assertion: switching between this and the
OpenAI-compatible adapter is three environment variables and a restart.
"""

from __future__ import annotations

import httpx

from ..config import get_settings
from .base import ProviderError, Usage

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

#: The one public base URL of the Google Generative Language API.
#:
#: D4 says no endpoints in the repository, and this is worth being explicit
#: about rather than quietly leaving in. The requirement qualifies it as
#: *environment-specific* configuration, and this is not: there is exactly one
#: of these, it is the same for every deployment, and it belongs to the adapter
#: in the same way the `/chat/completions` path belongs to the OpenAI-compatible
#: one. Nothing environment-specific -- no key, no project, no region, no model
#: -- is compiled in, and `LLM_BASE_URL` overrides this if a proxy or a
#: different API version is needed.
DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is not set. See .env.example.")
        if not settings.llm_model:
            raise RuntimeError("LLM_MODEL is not set, e.g. gemini-2.0-flash.")
        self.base_url = (settings.llm_base_url or DEFAULT_BASE).rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self._client = httpx.Client(timeout=httpx.Timeout(settings.llm_timeout_seconds))

    def complete(self, messages, *, json_mode, max_tokens, temperature):
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        contents = [
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": m.get("content", "")}],
            }
            for m in messages
            if m.get("role") != "system"
        ]
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        url = f"{self.base_url}/models/{self.model}:generateContent"
        try:
            response = self._client.post(url, json=payload, params={"key": self.api_key})
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport error: {exc}", retryable=True) from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"HTTP {response.status_code}: {response.text[:300]}",
                retryable=response.status_code in RETRYABLE_STATUS,
                status=response.status_code,
            )

        body = response.json()
        candidates = body.get("candidates") or []
        if not candidates:
            raise ProviderError(f"no candidates: {str(body)[:300]}", retryable=True)
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        meta = body.get("usageMetadata") or {}
        usage = Usage(
            prompt_tokens=int(meta.get("promptTokenCount") or 0),
            completion_tokens=int(meta.get("candidatesTokenCount") or 0),
        )
        return text, usage, self.model, body

    def healthy(self) -> bool:
        try:
            response = self._client.get(
                f"{self.base_url}/models", params={"key": self.api_key}, timeout=5.0
            )
            return response.status_code < 500
        except Exception:
            return False
