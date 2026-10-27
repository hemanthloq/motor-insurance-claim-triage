"""The provider interface (A5, L5, and the cost section of the brief).

Everything above this line in the stack talks to `LLMClient`, never to a vendor
SDK. That is deliberate: free tiers get rate limited, deprecated and withdrawn,
and when that happens we want to change one environment variable rather than
edit the agent. The interface is narrow on purpose -- a chat completion with an
optional JSON-object response format is all the agent needs.

Attached to every call, not bolted on afterwards:

* a timeout, always, on every call
* bounded retry with exponential backoff and full jitter
* a circuit breaker on the provider, shared across the worker process
* token and cost accounting attributed to the step that spent it
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import get_settings
from ..logging_setup import get_logger
from ..telemetry import (
    circuit_state,
    llm_calls,
    llm_cost,
    llm_latency,
    llm_retries,
    llm_tokens,
    span,
)

log = get_logger("llm")


class ProviderError(RuntimeError):
    """A call failed in a way that may be worth retrying."""

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class CircuitOpen(RuntimeError):
    """The breaker is open; the call was not attempted."""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Completion:
    text: str
    usage: Usage
    model: str
    provider: str
    latency_seconds: float
    attempts: int = 1
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        """Parse the body as JSON, tolerating the usual model output damage."""
        return parse_json_object(self.text)


class Provider(Protocol):
    name: str

    def complete(
        self, messages: list[dict[str, str]], *, json_mode: bool, max_tokens: int, temperature: float
    ) -> tuple[str, Usage, str, dict[str, Any]]:
        ...

    def healthy(self) -> bool:
        ...


# -----------------------------------------------------------------------------
# JSON repair
# -----------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from a model response.

    Models wrap JSON in fences, prefix it with "Here is the JSON:" and
    occasionally leave a trailing comma. None of that is worth a retry, so we
    repair the three cheap cases and fail loudly on anything else -- a silent
    `{}` here would become an unverifiable coverage position downstream.
    """
    if not text or not text.strip():
        raise ValueError("empty model response")
    candidate = text.strip()

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        inner = candidate[start : end + 1]
        try:
            parsed = json.loads(inner)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            repaired = re.sub(r",\s*([}\]])", r"\1", inner)
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed

    raise ValueError(f"model response is not a JSON object: {text[:300]!r}")


# -----------------------------------------------------------------------------
# Circuit breaker
# -----------------------------------------------------------------------------


class CircuitBreaker:
    """Three-state breaker, process-local.

    Process-local rather than shared through Redis on purpose. The thing we are
    protecting against is a provider that is down or rate-limiting us, and each
    worker observes that independently within a second or two. A shared breaker
    would add a Redis round trip to the hot path and a shared failure mode, to
    buy a few seconds of earlier tripping on the workers that have not yet seen
    an error themselves.

    It counts **attempts, not logical calls.** A `complete()` that retries
    three times against a dead provider sends three real requests and records
    three failures, so with the default `LLM_MAX_ATTEMPTS=3` and
    `CIRCUIT_FAIL_THRESHOLD=5` the breaker trips during the second failing
    claim rather than the fifth. That is the intent: the thing being rationed
    is requests to the provider, and grouping them inside one of our function
    calls does not make them cheaper for the provider. A consequence worth
    knowing is that a call can therefore raise `CircuitOpen` even though it did
    reach the provider -- the breaker tripped part-way through its own retries.
    The agent handles `CircuitOpen` as "provider unavailable, degrade and
    escalate", which is the right response either way.
    """

    CLOSED, HALF_OPEN, OPEN = 0, 1, 2

    def __init__(self, name: str, *, threshold: int, reset_seconds: float) -> None:
        self.name = name
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at = 0.0
        self._state = self.CLOSED
        self._lock = threading.Lock()
        circuit_state.labels(provider=name).set(self.CLOSED)

    @property
    def state(self) -> int:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state == self.OPEN and time.time() - self._opened_at >= self.reset_seconds:
            self._state = self.HALF_OPEN
            circuit_state.labels(provider=self.name).set(self.HALF_OPEN)
            log.info("circuit_half_open", provider=self.name)

    def before_call(self) -> None:
        with self._lock:
            self._maybe_half_open()
            if self._state == self.OPEN:
                remaining = self.reset_seconds - (time.time() - self._opened_at)
                raise CircuitOpen(
                    f"circuit open for provider {self.name}; "
                    f"retry in {max(0.0, remaining):.1f}s"
                )

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state != self.CLOSED:
                log.info("circuit_closed", provider=self.name)
            self._state = self.CLOSED
            circuit_state.labels(provider=self.name).set(self.CLOSED)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == self.HALF_OPEN or self._failures >= self.threshold:
                self._state = self.OPEN
                self._opened_at = time.time()
                circuit_state.labels(provider=self.name).set(self.OPEN)
                log.warning(
                    "circuit_opened",
                    provider=self.name,
                    consecutive_failures=self._failures,
                    reset_seconds=self.reset_seconds,
                )

    def force_open(self) -> None:
        """Demonstration hook (item 8). Trips the breaker without an outage."""
        with self._lock:
            self._state = self.OPEN
            self._opened_at = time.time()
            self._failures = self.threshold
            circuit_state.labels(provider=self.name).set(self.OPEN)
            log.warning("circuit_forced_open", provider=self.name)

    def reset(self) -> None:
        self.record_success()


# -----------------------------------------------------------------------------
# Client
# -----------------------------------------------------------------------------


class LLMClient:
    def __init__(self, provider: Provider | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.provider = provider or build_provider()
        self.breaker = CircuitBreaker(
            self.provider.name,
            threshold=settings.circuit_fail_threshold,
            reset_seconds=settings.circuit_reset_seconds,
        )

    # -- accounting -----------------------------------------------------------

    def _price(self, usage: Usage) -> float:
        settings = self.settings
        return (
            usage.prompt_tokens * settings.llm_price_prompt_per_mtok
            + usage.completion_tokens * settings.llm_price_completion_per_mtok
        ) / 1_000_000.0

    # -- call path ------------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        step: str,
        json_mode: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        settings = self.settings
        max_tokens = max_tokens or settings.llm_max_output_tokens
        temperature = settings.llm_temperature if temperature is None else temperature

        self.breaker.before_call()

        last_error: Exception | None = None
        started_total = time.perf_counter()

        for attempt in range(1, settings.llm_max_attempts + 1):
            started = time.perf_counter()
            try:
                with span(
                    "llm.complete",
                    **{
                        "llm.provider": self.provider.name,
                        "llm.step": step,
                        "llm.attempt": attempt,
                    },
                ):
                    text, usage, model, raw = self.provider.complete(
                        messages,
                        json_mode=json_mode,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
            except CircuitOpen:
                raise
            except Exception as exc:
                elapsed = time.perf_counter() - started
                llm_latency.labels(provider=self.provider.name).observe(elapsed)
                retryable = getattr(exc, "retryable", True)
                last_error = exc
                llm_calls.labels(provider=self.provider.name, outcome="error").inc()
                log.warning(
                    "llm_call_failed",
                    provider=self.provider.name,
                    step=step,
                    attempt=attempt,
                    retryable=retryable,
                    error=str(exc)[:300],
                )
                self.breaker.record_failure()
                if not retryable or attempt == settings.llm_max_attempts:
                    break
                llm_retries.labels(provider=self.provider.name).inc()
                # exponential backoff with full jitter: synchronised retries
                # from three workers are exactly what a rate-limited free tier
                # cannot absorb.
                ceiling = min(
                    settings.llm_backoff_max_seconds,
                    settings.llm_backoff_base_seconds * (2 ** (attempt - 1)),
                )
                time.sleep(random.uniform(0, ceiling))
                self.breaker.before_call()
                continue

            elapsed = time.perf_counter() - started
            usage.cost_usd = self._price(usage)
            llm_latency.labels(provider=self.provider.name).observe(elapsed)
            llm_calls.labels(provider=self.provider.name, outcome="success").inc()
            llm_tokens.labels(step=step, kind="prompt").inc(usage.prompt_tokens)
            llm_tokens.labels(step=step, kind="completion").inc(usage.completion_tokens)
            llm_cost.labels(step=step).inc(usage.cost_usd)
            self.breaker.record_success()

            return Completion(
                text=text,
                usage=usage,
                model=model,
                provider=self.provider.name,
                latency_seconds=time.perf_counter() - started_total,
                attempts=attempt,
                raw=raw,
            )

        raise ProviderError(
            f"{self.provider.name} failed after {settings.llm_max_attempts} attempts: {last_error}",
            retryable=False,
        )

    def healthy(self) -> bool:
        try:
            return self.provider.healthy()
        except Exception:
            return False


_client: LLMClient | None = None
_client_lock = threading.Lock()


def get_client() -> LLMClient:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = LLMClient()
    return _client


def reset_client() -> None:
    global _client
    _client = None


def build_provider() -> Provider:
    settings = get_settings()
    if settings.llm_provider == "stub":
        from .stub_provider import StubProvider

        return StubProvider()
    if settings.llm_provider == "gemini":
        from .gemini_provider import GeminiProvider

        return GeminiProvider()
    from .openai_compat import OpenAICompatibleProvider

    return OpenAICompatibleProvider()
