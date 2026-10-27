"""Any OpenAI-compatible /chat/completions endpoint.

One adapter covers Groq, OpenRouter, Together, DeepInfra, a local vLLM and
Ollama's /v1 shim. Which one is in use is a matter of three environment
variables, which is the point: the free tier that works this term may not work
next term.
"""

from __future__ import annotations

import httpx

from ..config import get_settings
from .base import ProviderError, Usage

#: Statuses worth another attempt. 429 and the 5xx family are transient;
#: 400/401/403/404 mean the request or the credential is wrong and retrying
#: just burns the budget faster.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class OpenAICompatibleProvider:
    name = "openai_compat"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.llm_base_url:
            raise RuntimeError(
                "LLM_BASE_URL is not set. See .env.example; no endpoint is "
                "compiled into this image."
            )
        if not settings.llm_model:
            raise RuntimeError("LLM_MODEL is not set. See .env.example.")
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.name = f"openai_compat:{_host(self.base_url)}"
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
            headers=headers,
        )

    def complete(self, messages, *, json_mode, max_tokens, temperature):
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = self._client.post(f"{self.base_url}/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"timeout after {self._client.timeout.read}s", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport error: {exc}", retryable=True) from exc

        if response.status_code >= 400:
            # Some gateways reject response_format rather than ignoring it.
            # Retrying once without it is cheaper than failing the run.
            if response.status_code == 400 and json_mode:
                payload.pop("response_format", None)
                response = self._client.post(f"{self.base_url}/chat/completions", json=payload)
            if response.status_code >= 400:
                raise ProviderError(
                    f"HTTP {response.status_code}: {response.text[:300]}",
                    retryable=response.status_code in RETRYABLE_STATUS,
                    status=response.status_code,
                )

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError(f"no choices in response: {str(body)[:300]}", retryable=True)
        text = (choices[0].get("message") or {}).get("content") or ""
        raw_usage = body.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
            completion_tokens=int(raw_usage.get("completion_tokens") or 0),
        )
        if not usage.prompt_tokens:
            # Not every gateway returns usage. An estimate attributed to the
            # right step beats a zero that quietly understates the cost report.
            usage.prompt_tokens = _estimate(messages)
            usage.completion_tokens = max(1, len(text) // 4)
        return text, usage, body.get("model", self.model), body

    def healthy(self) -> bool:
        try:
            response = self._client.get(f"{self.base_url}/models", timeout=5.0)
            return response.status_code < 500
        except Exception:
            return False


def _estimate(messages: list[dict[str, str]]) -> int:
    return max(1, sum(len(m.get("content", "")) for m in messages) // 4)


def _host(url: str) -> str:
    return url.split("//")[-1].split("/")[0]
