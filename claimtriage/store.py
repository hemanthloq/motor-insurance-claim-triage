"""Durable state: the queue, the run store, quarantine, feedback and the caches.

Redis is the single stateful dependency of the request path. It carries four
distinct things, which are kept in separate key namespaces so they can be split
onto separate instances later without touching call sites:

  triage:queue            list  -- the work queue between API and worker (A4)
  triage:run:<id>         hash  -- run state and the final result
  triage:quarantine:*     hash  -- rejected submissions with field-level errors
  triage:feedback:*       hash  -- caller verdicts (L6)
  triage:cache:*          string-- embedding / retrieval / whole-run caches (D7)
  triage:rl:<client>      string-- fixed-window rate limit counters (D7)

`InMemoryBackend` implements the same surface so that `EXECUTION_MODE=inline`
and the unit tests run with no infrastructure at all.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .config import get_settings
from .logging_setup import get_logger
from .telemetry import cache_events, queue_depth, rate_limit_events

log = get_logger("store")

QUEUE_KEY_TMPL = "{queue}"
RUN_KEY = "triage:run:{run_id}"
RUN_INDEX = "triage:runs"
IDEMPOTENCY_KEY = "triage:idem:{reference}"
QUARANTINE_KEY = "triage:quarantine:{qid}"
QUARANTINE_INDEX = "triage:quarantine"
FEEDBACK_KEY = "triage:feedback:{fid}"
FEEDBACK_INDEX = "triage:feedback"
CACHE_KEY = "triage:cache:{layer}:{digest}"
RATE_KEY = "triage:rl:{client}:{window}"
INDEX_META_KEY = "triage:index:meta"
LIVE_SAMPLE_KEY = "triage:quality:samples"


# -----------------------------------------------------------------------------
# Backends
# -----------------------------------------------------------------------------


class InMemoryBackend:
    """Process-local stand-in for Redis. Not shared between containers."""

    def __init__(self) -> None:
        self._kv: dict[str, Any] = {}
        self._expiry: dict[str, float] = {}
        self._lists: dict[str, deque] = {}
        self._lock = threading.RLock()

    def _sweep(self) -> None:
        now = time.time()
        for key, exp in list(self._expiry.items()):
            if exp <= now:
                self._kv.pop(key, None)
                self._expiry.pop(key, None)

    def get(self, key: str) -> str | None:
        with self._lock:
            self._sweep()
            return self._kv.get(key)

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        with self._lock:
            self._sweep()
            if nx and key in self._kv:
                return False
            self._kv[key] = value
            if ex:
                self._expiry[key] = time.time() + ex
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._kv.pop(key, None)
            self._expiry.pop(key, None)

    def incr(self, key: str) -> int:
        with self._lock:
            self._sweep()
            value = int(self._kv.get(key, 0)) + 1
            self._kv[key] = str(value)
            return value

    def expire(self, key: str, seconds: int) -> None:
        with self._lock:
            self._expiry[key] = time.time() + seconds

    def rpush(self, key: str, value: str) -> int:
        with self._lock:
            self._lists.setdefault(key, deque()).append(value)
            return len(self._lists[key])

    def lpush(self, key: str, value: str) -> int:
        with self._lock:
            self._lists.setdefault(key, deque()).appendleft(value)
            return len(self._lists[key])

    def blpop(self, key: str, timeout: int = 1) -> tuple[str, str] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                bucket = self._lists.get(key)
                if bucket:
                    return key, bucket.popleft()
            time.sleep(0.02)
        return None

    def llen(self, key: str) -> int:
        with self._lock:
            return len(self._lists.get(key, ()))

    def zadd(self, key: str, member: str, score: float) -> None:
        with self._lock:
            table = self._kv.setdefault(f"__z__{key}", {})
            table[member] = score

    def zrevrange(self, key: str, start: int, stop: int) -> list[str]:
        with self._lock:
            table = self._kv.get(f"__z__{key}", {})
            ordered = sorted(table.items(), key=lambda kv: kv[1], reverse=True)
            end = None if stop < 0 else stop + 1
            return [m for m, _ in ordered[start:end]]

    def zcard(self, key: str) -> int:
        with self._lock:
            return len(self._kv.get(f"__z__{key}", {}))

    def ping(self) -> bool:
        return True


class RedisBackend:
    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key):
        return self._client.get(key)

    def set(self, key, value, ex=None, nx=False):
        return bool(self._client.set(key, value, ex=ex, nx=nx))

    def delete(self, key):
        self._client.delete(key)

    def incr(self, key):
        return int(self._client.incr(key))

    def expire(self, key, seconds):
        self._client.expire(key, seconds)

    def rpush(self, key, value):
        return int(self._client.rpush(key, value))

    def lpush(self, key, value):
        return int(self._client.lpush(key, value))

    def blpop(self, key, timeout=1):
        result = self._client.blpop(key, timeout=timeout)
        return (result[0], result[1]) if result else None

    def llen(self, key):
        return int(self._client.llen(key))

    def zadd(self, key, member, score):
        self._client.zadd(key, {member: score})

    def zrevrange(self, key, start, stop):
        return list(self._client.zrevrange(key, start, stop))

    def zcard(self, key):
        return int(self._client.zcard(key))

    def ping(self):
        return bool(self._client.ping())


_backend: Any = None
_backend_lock = threading.Lock()


def get_backend() -> Any:
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        settings = get_settings()
        if settings.execution_mode == "inline" and settings.redis_url.startswith("memory"):
            _backend = InMemoryBackend()
            return _backend
        try:
            backend = RedisBackend(settings.redis_url)
            backend.ping()
            _backend = backend
        except Exception as exc:
            log.warning(
                "redis_unavailable_falling_back_to_memory",
                error=str(exc),
                consequence="state is process-local; do not do this in production",
            )
            _backend = InMemoryBackend()
        return _backend


def reset_backend() -> None:
    """Test hook."""
    global _backend
    _backend = None


def backend_healthy() -> bool:
    try:
        return bool(get_backend().ping())
    except Exception:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------------------------------------------------------
# Queue (A4)
# -----------------------------------------------------------------------------


def enqueue(job: dict[str, Any]) -> int:
    settings = get_settings()
    depth = get_backend().rpush(settings.queue_name, json.dumps(job, default=str))
    queue_depth.set(depth)
    return depth


def dequeue(timeout: int = 2) -> dict[str, Any] | None:
    settings = get_settings()
    item = get_backend().blpop(settings.queue_name, timeout=timeout)
    if not item:
        return None
    queue_depth.set(get_backend().llen(settings.queue_name))
    try:
        return json.loads(item[1])
    except json.JSONDecodeError:
        log.error("queue_poison_message", raw=item[1][:500])
        return None


def current_queue_depth() -> int:
    try:
        depth = get_backend().llen(get_settings().queue_name)
        queue_depth.set(depth)
        return depth
    except Exception:
        return -1


# -----------------------------------------------------------------------------
# Run store
# -----------------------------------------------------------------------------


def save_run(run_id: str, document: dict[str, Any]) -> None:
    settings = get_settings()
    get_backend().set(
        RUN_KEY.format(run_id=run_id),
        json.dumps(document, default=str),
        ex=settings.run_ttl_seconds,
    )
    get_backend().zadd(RUN_INDEX, run_id, time.time())


def load_run(run_id: str) -> dict[str, Any] | None:
    raw = get_backend().get(RUN_KEY.format(run_id=run_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def patch_run(run_id: str, **fields: Any) -> dict[str, Any] | None:
    document = load_run(run_id)
    if document is None:
        return None
    document.update(fields)
    document["updated_at"] = _now()
    save_run(run_id, document)
    return document


def recent_runs(limit: int = 50) -> list[dict[str, Any]]:
    ids = get_backend().zrevrange(RUN_INDEX, 0, limit - 1)
    out = []
    for run_id in ids:
        doc = load_run(run_id)
        if doc:
            out.append(doc)
    return out


def claim_idempotency(reference: str, run_id: str, ttl: int) -> str | None:
    """Return an existing run id for this reference, or None if we claimed it."""
    key = IDEMPOTENCY_KEY.format(reference=reference)
    if get_backend().set(key, run_id, ex=ttl, nx=True):
        return None
    return get_backend().get(key)


# -----------------------------------------------------------------------------
# Quarantine (D1)
# -----------------------------------------------------------------------------


def save_quarantine(record: dict[str, Any]) -> None:
    qid = record["quarantine_id"]
    get_backend().set(
        QUARANTINE_KEY.format(qid=qid),
        json.dumps(record, default=str),
        ex=get_settings().run_ttl_seconds * 7,
    )
    get_backend().zadd(QUARANTINE_INDEX, qid, time.time())


def load_quarantine(qid: str) -> dict[str, Any] | None:
    raw = get_backend().get(QUARANTINE_KEY.format(qid=qid))
    return json.loads(raw) if raw else None


def recent_quarantine(limit: int = 50) -> list[dict[str, Any]]:
    out = []
    for qid in get_backend().zrevrange(QUARANTINE_INDEX, 0, limit - 1):
        record = load_quarantine(qid)
        if record:
            out.append(record)
    return out


def quarantine_count() -> int:
    return get_backend().zcard(QUARANTINE_INDEX)


# -----------------------------------------------------------------------------
# Feedback (L6)
# -----------------------------------------------------------------------------


def save_feedback(record: dict[str, Any]) -> None:
    fid = record["feedback_id"]
    get_backend().set(
        FEEDBACK_KEY.format(fid=fid),
        json.dumps(record, default=str),
        ex=get_settings().run_ttl_seconds * 30,
    )
    get_backend().zadd(FEEDBACK_INDEX, fid, time.time())


def recent_feedback(limit: int = 200) -> list[dict[str, Any]]:
    out = []
    for fid in get_backend().zrevrange(FEEDBACK_INDEX, 0, limit - 1):
        raw = get_backend().get(FEEDBACK_KEY.format(fid=fid))
        if raw:
            out.append(json.loads(raw))
    return out


def record_quality_sample(sample: dict[str, Any]) -> None:
    """Append one automatically-scored live run (L6)."""
    key = f"{LIVE_SAMPLE_KEY}:{sample['run_id']}"
    get_backend().set(key, json.dumps(sample, default=str), ex=get_settings().run_ttl_seconds * 7)
    get_backend().zadd(LIVE_SAMPLE_KEY, sample["run_id"], time.time())


def quality_samples(limit: int = 200) -> list[dict[str, Any]]:
    out = []
    for run_id in get_backend().zrevrange(LIVE_SAMPLE_KEY, 0, limit - 1):
        raw = get_backend().get(f"{LIVE_SAMPLE_KEY}:{run_id}")
        if raw:
            out.append(json.loads(raw))
    return out


# -----------------------------------------------------------------------------
# Cache (D7)
# -----------------------------------------------------------------------------


def cache_get(layer: str, digest: str) -> Any | None:
    key = CACHE_KEY.format(layer=layer, digest=digest)
    raw = get_backend().get(key)
    if raw is None:
        cache_events.labels(layer=layer, event="miss").inc()
        return None
    cache_events.labels(layer=layer, event="hit").inc()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def cache_put(layer: str, digest: str, value: Any, ttl: int | None = None) -> None:
    settings = get_settings()
    get_backend().set(
        CACHE_KEY.format(layer=layer, digest=digest),
        json.dumps(value, default=str),
        ex=ttl or settings.cache_ttl_seconds,
    )


# -----------------------------------------------------------------------------
# Rate limiting (D7)
# -----------------------------------------------------------------------------


def rate_limit_check(client_id: str) -> tuple[bool, int, int]:
    """Fixed-window counter. Returns (allowed, remaining, reset_in_seconds).

    A fixed window is the right trade here: it costs one INCR, it cannot be
    gamed badly at the request volumes a claims intake sees, and the thing we
    are actually defending is the model budget, not fairness at the millisecond.
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return True, settings.rate_limit_requests, 0

    window_size = settings.rate_limit_window_seconds
    window = int(time.time() // window_size)
    key = RATE_KEY.format(client=client_id, window=window)
    count = get_backend().incr(key)
    if count == 1:
        get_backend().expire(key, window_size + 1)
    reset_in = window_size - int(time.time() % window_size)
    if count > settings.rate_limit_requests:
        rate_limit_events.labels(client=client_id[:40]).inc()
        return False, 0, reset_in
    return True, settings.rate_limit_requests - count, reset_in


# -----------------------------------------------------------------------------
# Index metadata (D2)
# -----------------------------------------------------------------------------


def save_index_meta(meta: dict[str, Any]) -> None:
    get_backend().set(INDEX_META_KEY, json.dumps(meta, default=str))


def load_index_meta() -> dict[str, Any] | None:
    raw = get_backend().get(INDEX_META_KEY)
    return json.loads(raw) if raw else None
