"""In-process stub provider.

Selected with `LLM_PROVIDER=stub`. Used by the unit tests and by the no-network
demonstration path. The load test does *not* use this one -- it points the
ordinary OpenAI-compatible adapter at the `llm-stub` container, so that the HTTP
hop, the connection pool and the timeout logic are all exercised under load.
"""

from __future__ import annotations

import random
import time

from ..config import get_settings
from .base import ProviderError, Usage
from .stub_logic import respond, usage_for


class StubProvider:
    name = "stub"

    def __init__(self) -> None:
        settings = get_settings()
        self.latency_ms = settings.stub_latency_ms
        self.jitter_ms = settings.stub_latency_jitter_ms
        self.failure_rate = settings.stub_failure_rate

    def complete(self, messages, *, json_mode, max_tokens, temperature):
        prompt = "\n".join(m.get("content", "") for m in messages)
        delay = (self.latency_ms + random.uniform(0, self.jitter_ms)) / 1000.0
        time.sleep(delay)
        if self.failure_rate and random.random() < self.failure_rate:
            raise ProviderError("stub: injected failure", retryable=True, status=503)
        body = respond(prompt)
        counts = usage_for(prompt, body)
        usage = Usage(
            prompt_tokens=counts["prompt_tokens"],
            completion_tokens=counts["completion_tokens"],
        )
        return body, usage, "stub-1", {"stub": True}

    def healthy(self) -> bool:
        return True
