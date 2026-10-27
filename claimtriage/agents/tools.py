"""The tools the agent may call (A1).

Five tools, in two families.

Search
    `corpus_search`        hybrid search, optionally filtered by clause role
    `find_named_clause`    resolve a clause the wording refers to *by name*

Navigation
    `get_clause`           fetch one clause by its identifier
    `clause_neighbours`    the clauses either side of one, in document order
    `get_claim_field`      read a field of the request under triage

The navigation family is the interesting half. Search alone cannot solve the
section 4.1 case reliably: the governing restriction does not resemble the
question. But the general exceptions clause *names* it -- "otherwise than in
accordance with the 'Limitations as to Use'" -- and the chunker recorded that
cross-reference at index time. So the agent can read one clause, see what it
points at, and go and fetch that, which is how a claims handler actually works
through a wording.

Every tool call is bounded by `TOOL_TIMEOUT_SECONDS`, counted against the run's
tool budget, timed, traced, and passed through the tool-surface guardrail before
its text is allowed near a prompt.
"""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import get_settings
from ..guardrails import check_tool_output
from ..logging_setup import get_logger
from ..retrieval.index import ClauseIndex, SearchHit, get_index
from ..telemetry import span, tool_calls, tool_latency

log = get_logger("tools")


class ToolBudgetExceeded(RuntimeError):
    """The run has spent its tool-call allowance (A1)."""


class ToolTimeout(RuntimeError):
    pass


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    duration_ms: float
    outcome: str
    result_ids: list[str] = field(default_factory=list)
    top_score: float | None = None
    error: str | None = None


@dataclass
class ToolContext:
    """Per-run tool state: the budget, the claim, and what has been retrieved."""

    claim: dict[str, Any]
    index: ClauseIndex
    max_calls: int
    deadline: float
    calls: list[ToolCall] = field(default_factory=list)
    retrieved: dict[str, SearchHit] = field(default_factory=dict)
    queries_issued: list[str] = field(default_factory=list)
    guardrail_findings: list[dict] = field(default_factory=list)

    @property
    def calls_made(self) -> int:
        return len(self.calls)

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.calls_made)

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def record(self, hits: list[SearchHit]) -> list[SearchHit]:
        fresh = []
        for hit in hits:
            if hit.clause_id not in self.retrieved:
                self.retrieved[hit.clause_id] = hit
                fresh.append(hit)
            elif hit.score > self.retrieved[hit.clause_id].score:
                self.retrieved[hit.clause_id] = hit
        return fresh


_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


def _invoke(ctx: ToolContext, name: str, arguments: dict[str, Any], fn: Callable[[], Any]) -> Any:
    settings = get_settings()
    if ctx.remaining_calls <= 0:
        tool_calls.labels(tool=name, outcome="budget_exceeded").inc()
        raise ToolBudgetExceeded(
            f"tool budget of {ctx.max_calls} calls exhausted before {name}"
        )
    if ctx.time_left() <= 0:
        tool_calls.labels(tool=name, outcome="deadline_exceeded").inc()
        raise ToolTimeout(f"run wall-clock budget exhausted before {name}")

    started = time.perf_counter()
    timeout = min(settings.tool_timeout_seconds, max(0.1, ctx.time_left()))
    outcome, error, result = "success", None, None
    try:
        with span(f"tool.{name}", **{f"tool.arg.{k}": str(v)[:120] for k, v in arguments.items()}):
            result = _executor.submit(fn).result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        outcome, error = "timeout", f"exceeded {timeout:.1f}s"
        raise ToolTimeout(f"{name} {error}") from exc
    except Exception as exc:
        outcome, error = "error", str(exc)[:300]
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        tool_latency.labels(tool=name).observe(duration_ms / 1000)
        tool_calls.labels(tool=name, outcome=outcome).inc()
        ids, top = _summarise(result)
        ctx.calls.append(
            ToolCall(
                name=name,
                arguments=arguments,
                duration_ms=round(duration_ms, 2),
                outcome=outcome,
                result_ids=ids,
                top_score=top,
                error=error,
            )
        )
        log.info(
            "tool_call",
            tool=name,
            outcome=outcome,
            duration_ms=round(duration_ms, 2),
            results=len(ids),
            top_score=top,
            **{f"arg_{k}": str(v)[:120] for k, v in arguments.items()},
        )
    return result


def _summarise(result: Any) -> tuple[list[str], float | None]:
    if isinstance(result, list) and result and isinstance(result[0], SearchHit):
        return [h.clause_id for h in result], round(max(h.score for h in result), 4)
    if isinstance(result, SearchHit):
        return [result.clause_id], round(result.score, 4)
    return [], None


def _sanitise(ctx: ToolContext, hits: list[SearchHit]) -> list[SearchHit]:
    """Tool-surface guardrail (L4): text from the corpus is screened before it
    is shown to a model that holds tools.

    A passage that trips a high-confidence injection pattern is dropped
    entirely. A passage is never rewritten -- see the note in
    `guardrails.check_tool_output`: the model quotes these passages verbatim
    and the verifier checks those quotes against the stored clause, so
    altering the copy the model sees would break citation resolution.
    """
    kept = []
    for hit in hits:
        result = check_tool_output(hit.text)
        if result.triggered:
            ctx.guardrail_findings.extend(f.to_dict() for f in result.findings)
        if not result.allowed:
            log.warning(
                "retrieved_passage_dropped_by_guardrail",
                clause_id=hit.clause_id,
                patterns=[f.pattern for f in result.findings],
            )
            ctx.retrieved.pop(hit.clause_id, None)
            continue
        kept.append(hit)
    return kept


# -----------------------------------------------------------------------------
# Tool implementations
# -----------------------------------------------------------------------------


def corpus_search(
    ctx: ToolContext,
    query: str,
    *,
    role: str | list[str] | None = None,
    top_k: int | None = None,
    strategy: str = "hybrid",
) -> list[SearchHit]:
    """Search the indexed policy corpus.

    `role` narrows to one kind of provision -- restriction, exclusion,
    condition, grant, procedure, definition. Filtering by role is how the agent
    asks the question "what, anywhere in this wording, could take the cover
    away?", which is not a question any phrasing of the claim description will
    answer.
    """
    top_k = top_k or get_settings().retrieval_top_k
    hits = _invoke(
        ctx,
        "corpus_search",
        {"query": query, "role": role, "top_k": top_k, "strategy": strategy},
        lambda: ctx.index.search(query, top_k=top_k, role=role, strategy=strategy),
    )
    ctx.queries_issued.append(query)
    ctx.record(hits)
    return _sanitise(ctx, hits)


def find_named_clause(ctx: ToolContext, name: str) -> list[SearchHit]:
    """Resolve a clause the wording refers to by name.

    The second-order retrieval step. When a retrieved exception says the cover
    does not apply where the vehicle is used "otherwise than in accordance with
    the 'Limitations as to Use'", this is how the agent goes and reads the
    Limitations as to Use.
    """
    hits = _invoke(
        ctx,
        "find_named_clause",
        {"name": name},
        lambda: ctx.index.find_named_clause(name)[: get_settings().retrieval_top_k],
    )
    ctx.record(hits)
    return _sanitise(ctx, hits)


def get_clause(ctx: ToolContext, clause_id: str) -> SearchHit | None:
    """Fetch one clause by its identifier."""
    hit = _invoke(ctx, "get_clause", {"clause_id": clause_id}, lambda: ctx.index.get_clause(clause_id))
    if hit:
        ctx.record([hit])
        _sanitise(ctx, [hit])
    return hit


def clause_neighbours(ctx: ToolContext, clause_id: str, window: int = 1) -> list[SearchHit]:
    """The clauses immediately before and after one, in document order.

    Policy wordings put the proviso in the next sentence and the carve-out in
    the next sub-clause. Reading a clause without its neighbours is how you end
    up citing a prohibition whose very next line disapplies it.
    """
    hits = _invoke(
        ctx,
        "clause_neighbours",
        {"clause_id": clause_id, "window": window},
        lambda: ctx.index.neighbours(clause_id, window=window),
    )
    ctx.record(hits)
    return _sanitise(ctx, hits)


def get_claim_field(ctx: ToolContext, path: str) -> Any:
    """Read a field of the request under triage, by dotted path.

    The agent should not be re-deriving `incident.vehicle_use_at_time_of_loss`
    out of the free text when the contract carries it as a validated enum.
    """

    def _read() -> Any:
        node: Any = ctx.claim
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    return _invoke(ctx, "get_claim_field", {"path": path}, _read)


TOOL_SPECS = [
    {
        "name": "corpus_search",
        "description": "Hybrid dense+lexical search over the indexed policy corpus. Optional role filter: grant, restriction, exclusion, condition, procedure, definition.",
        "parameters": {"query": "str", "role": "str|list[str]|None", "top_k": "int|None"},
    },
    {
        "name": "find_named_clause",
        "description": "Resolve a clause that the wording refers to by name, e.g. 'Limitations as to Use'.",
        "parameters": {"name": "str"},
    },
    {
        "name": "get_clause",
        "description": "Fetch one clause by its clause identifier.",
        "parameters": {"clause_id": "str"},
    },
    {
        "name": "clause_neighbours",
        "description": "Fetch the clauses adjacent to a given clause in document order.",
        "parameters": {"clause_id": "str", "window": "int"},
    },
    {
        "name": "get_claim_field",
        "description": "Read a validated field of the FNOL under triage, by dotted path.",
        "parameters": {"path": "str"},
    },
]
