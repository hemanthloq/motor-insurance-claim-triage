"""Turn a graph run into a `TriageResult` (A3, D5, D7, L4, L5).

Three things happen here that do not belong inside the graph.

**Citations are filtered, not trusted.** Only citations whose quote resolved
character-for-character against the clause they name are carried into the
result. A citation that did not resolve is dropped and recorded in the
verification report. "Every citation must resolve to retrieved text" is
therefore a property of the response object, not an instruction in a prompt.

**The output guardrail runs last.** After everything else, the prose the caller
will actually read is checked for determinations the service is not authorised
to make. If it fires, the run is converted to an escalation: the model may have
written "we will settle this claim", but that sentence never leaves the process.

**The whole run is cached, keyed on what could change the answer.** The claim
body, the prompt-set fingerprint and the index version. Not the run id, not the
timestamp.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from ..config import get_settings
from ..contracts.models import (
    Citation,
    ClaimType,
    Classification,
    CostBreakdown,
    CoverageDetermination,
    CoveragePosition,
    EscalationNotice,
    EscalationReason,
    RoutingDecision,
    RoutingQueue,
    RunStatus,
    StepRecord,
    TriageResult,
    VerificationReport,
)
from ..correlation import set_correlation_id, set_run_id, set_step
from ..guardrails import check_output
from ..llm import LLMClient
from ..logging_setup import get_logger
from ..prompts import get_registry
from ..retrieval.index import get_index, read_index_meta
from ..store import cache_get, cache_put
from ..telemetry import (
    current_trace_id,
    inflight_runs,
    routing_decisions,
    run_latency,
    run_steps,
    run_tool_calls,
    runs_finished,
    runs_started,
    span,
)
from .graph import AgentRunner

log = get_logger("runner")


def run_cache_key(claim: dict[str, Any]) -> str:
    """What makes two runs the same run.

    The claim body, the prompts in force and the index version. Deliberately
    *not* the claim reference: two different references describing the same
    incident should hit the cache, and the same reference submitted after a
    re-index should not.
    """
    settings = get_settings()
    body = json.dumps(
        {k: v for k, v in claim.items() if k not in {"claim_reference", "reported_at"}},
        sort_keys=True,
        default=str,
    )
    meta = read_index_meta()
    material = "|".join(
        [
            body,
            settings.prompt_set,
            get_registry().fingerprint(),
            str(meta.get("index_version", "0")),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def execute_run(
    *,
    run_id: str,
    correlation_id: str,
    claim: dict[str, Any],
    warnings: list[dict[str, Any]] | None = None,
    submitted_at: datetime | None = None,
    client: LLMClient | None = None,
    index: Any = None,
    prompt_set: str | None = None,
    use_cache: bool | None = None,
) -> TriageResult:
    settings = get_settings()
    set_correlation_id(correlation_id)
    set_run_id(run_id)
    set_step("start")
    submitted_at = submitted_at or datetime.now(timezone.utc)
    started = time.perf_counter()

    use_cache = settings.cache_runs if use_cache is None else use_cache
    cache_key = run_cache_key(claim) if use_cache else None
    if cache_key:
        cached = cache_get("run", cache_key)
        if cached:
            log.info("run_cache_hit", run_id=run_id, cache_key=cache_key)
            result = TriageResult.model_validate(cached)
            result.cached_from_run_id = result.run_id
            result.run_id = run_id
            result.correlation_id = correlation_id
            result.submitted_at = submitted_at
            result.completed_at = datetime.now(timezone.utc)
            result.duration_seconds = round(time.perf_counter() - started, 4)
            result.served_from_cache = True
            # A cached run makes no model calls, so it costs nothing. Replaying
            # the originating run's token count here would inflate the cost
            # report by exactly the amount the cache saved, and make the D7
            # cost benefit invisible -- the one number the cache exists for.
            result.cost = CostBreakdown()
            result.steps = []
            result.tool_calls = 0
            run_latency.observe(result.duration_seconds)
            runs_finished.labels(status=result.status.value).inc()
            routing_decisions.labels(
                queue=result.routing.queue.value if result.routing else "unknown"
            ).inc()
            return result

    runs_started.inc()
    inflight_runs.inc()
    try:
        runner = AgentRunner(
            run_id=run_id,
            correlation_id=correlation_id,
            claim=claim,
            warnings=warnings or [],
            client=client,
            index=index,
            prompt_set=prompt_set,
        )
        state = runner.run()
        result = _assemble(
            runner=runner,
            state=state,
            run_id=run_id,
            correlation_id=correlation_id,
            claim=claim,
            submitted_at=submitted_at,
            started=started,
        )
    except Exception as exc:  # the graph itself failed -- never return a guess
        log.exception("run_failed", run_id=run_id, error=str(exc))
        runs_finished.labels(status="failed").inc()
        duration = round(time.perf_counter() - started, 4)
        run_latency.observe(duration)
        return TriageResult(
            run_id=run_id,
            correlation_id=correlation_id,
            claim_reference=str(claim.get("claim_reference", "")),
            status=RunStatus.failed,
            submitted_at=submitted_at,
            completed_at=datetime.now(timezone.utc),
            duration_seconds=duration,
            escalation=EscalationNotice(
                required=True,
                reason=EscalationReason.verification_failed,
                explanation=f"the run did not complete: {exc}",
            ),
            routing=RoutingDecision(
                queue=RoutingQueue.human_escalation,
                reason="the automated run failed; a human handler must triage this claim",
                priority="high",
                target_first_touch_hours=4,
            ),
            error=str(exc)[:500],
            trace_id=current_trace_id(),
        )
    finally:
        inflight_runs.dec()
        set_step("-")

    if cache_key and result.status == RunStatus.completed and not result.degraded:
        cache_put("run", cache_key, result.model_dump(mode="json"))
    return result


def _assemble(
    *,
    runner: AgentRunner,
    state: dict[str, Any],
    run_id: str,
    correlation_id: str,
    claim: dict[str, Any],
    submitted_at: datetime,
    started: float,
) -> TriageResult:
    draft = state.get("draft") or {}
    verification = state.get("verification") or {}
    routing_raw = state.get("routing") or {}
    escalation_raw = state.get("escalation")

    # ---- citations: only what resolved ------------------------------------
    resolved = verification.get("resolved_citations")
    if resolved is None:
        resolved = [
            c
            for c in (draft.get("citations") or [])
            if isinstance(c, dict)
            and runner.index.quote_resolves(str(c.get("clause_id", "")), str(c.get("quote", "")))
        ]
    citations: list[Citation] = []
    for raw in resolved:
        hit = runner.ctx.retrieved.get(raw.get("clause_id")) or runner.index.get_clause(
            str(raw.get("clause_id"))
        )
        if hit is None:
            continue
        role = raw.get("role") if raw.get("role") in {
            "grant", "restriction", "exclusion", "condition", "procedure", "definition"
        } else hit.role
        citations.append(
            Citation(
                clause_id=hit.clause_id,
                document_id=hit.document_id,
                document_title=hit.document_title,
                section_path=hit.section_path,
                page=hit.page or None,
                quote=str(raw.get("quote", ""))[:1200],
                relevance=round(min(1.0, max(0.0, hit.score)), 4),
                role=role,
                resolves=True,
            )
        )

    # ---- classification ----------------------------------------------------
    classification_raw = state.get("classification") or {}
    classification = None
    if classification_raw:
        try:
            claim_type = ClaimType(classification_raw.get("claim_type", "undetermined"))
        except ValueError:
            claim_type = ClaimType.undetermined
        secondary = []
        for value in classification_raw.get("secondary_types") or []:
            try:
                secondary.append(ClaimType(value))
            except (ValueError, TypeError):
                continue
        classification = Classification(
            claim_type=claim_type,
            confidence=_clamp(classification_raw.get("confidence")),
            secondary_types=secondary,
            entities=classification_raw.get("entities") or {},
            rationale=str(classification_raw.get("rationale", ""))[:1000],
        )

    # ---- coverage ----------------------------------------------------------
    coverage = None
    if draft:
        try:
            position = CoveragePosition(draft.get("position", "undetermined"))
        except ValueError:
            position = CoveragePosition.undetermined
        coverage = CoverageDetermination(
            position=position,
            summary=str(draft.get("summary", ""))[:1000],
            reasoning=str(draft.get("reasoning", ""))[:4000],
            governing_clauses=[str(c) for c in (draft.get("governing_clauses") or [])][:10],
            modifying_clauses=[str(c) for c in (draft.get("modifying_clauses") or [])][:10],
            open_questions=[str(q) for q in (draft.get("open_questions") or [])][:10],
            confidence=_clamp(draft.get("confidence")),
        )

    # ---- verification ------------------------------------------------------
    report = VerificationReport(
        verdict=verification.get("verdict", "not_run")
        if verification.get("verdict") in {"verified", "unsupported", "not_run"}
        else "not_run",
        support_score=_clamp(verification.get("support_score")),
        citations_checked=int(verification.get("citations_checked") or 0),
        citations_resolved=int(verification.get("citations_resolved") or 0),
        unsupported_statements=[
            str(s) for s in (verification.get("unsupported_statements") or [])
        ][:10],
        attempts=int(verification.get("attempts") or 0),
        notes=str(verification.get("notes", ""))[:1000],
    )

    # ---- routing -----------------------------------------------------------
    routing = None
    if routing_raw:
        try:
            queue = RoutingQueue(routing_raw.get("queue"))
        except ValueError:
            queue = RoutingQueue.human_escalation
        routing = RoutingDecision(
            queue=queue,
            reason=str(routing_raw.get("reason", ""))[:1000],
            priority=routing_raw.get("priority", "normal"),
            target_first_touch_hours=int(routing_raw.get("target_first_touch_hours") or 24),
            triggered_rules=[str(r) for r in (routing_raw.get("triggered_rules") or [])][:20],
        )

    escalation = (
        EscalationNotice.model_validate(escalation_raw) if escalation_raw else EscalationNotice()
    )

    # ---- cost (L5) ---------------------------------------------------------
    per_step = {k: round(v, 8) for k, v in (state.get("cost_per_step") or {}).items()}
    tokens_per_step = {k: int(v) for k, v in (state.get("tokens_per_step") or {}).items()}
    tokens = state.get("tokens") or {"prompt": 0, "completion": 0}

    # The largest cost driver is read off spend when a price list is configured
    # and off tokens when it is not. A free tier makes every spend figure 0.00,
    # and `max()` over a dict of equal values returns whichever key happens to
    # be first -- which reported `intake_classification` as the dominant step
    # when it is in fact the cheapest of the five by a factor of six.
    if per_step and any(v > 0 for v in per_step.values()):
        largest, basis = max(per_step, key=lambda k: per_step[k]), "cost"
    elif tokens_per_step:
        largest, basis = max(tokens_per_step, key=lambda k: tokens_per_step[k]), "tokens"
    else:
        largest, basis = None, "none"

    cost = CostBreakdown(
        prompt_tokens=int(tokens.get("prompt", 0)),
        completion_tokens=int(tokens.get("completion", 0)),
        total_tokens=int(tokens.get("prompt", 0)) + int(tokens.get("completion", 0)),
        cost_usd=round(sum(per_step.values()), 8),
        per_step=per_step,
        tokens_per_step=tokens_per_step,
        largest_driver=largest,
        largest_driver_basis=basis,  # type: ignore[arg-type]
    )

    status = (
        RunStatus.escalated
        if (escalation.required or state.get("terminal") == "escalated")
        else RunStatus.completed
    )

    steps = [StepRecord.model_validate(s) for s in (state.get("steps") or [])]
    duration = round(time.perf_counter() - started, 4)

    result = TriageResult(
        run_id=run_id,
        correlation_id=correlation_id,
        claim_reference=str(claim.get("claim_reference", "")),
        status=status,
        submitted_at=submitted_at,
        completed_at=datetime.now(timezone.utc),
        duration_seconds=duration,
        classification=classification,
        coverage=coverage,
        routing=routing,
        verification=report,
        escalation=escalation,
        citations=citations,
        steps=steps,
        tool_calls=runner.ctx.calls_made,
        prompt_versions=state.get("prompt_versions") or {},
        cost=cost,
        index_version=read_index_meta().get("index_version"),
        trace_id=current_trace_id(),
        degraded=bool(state.get("degraded")),
        degradation_path=list(state.get("degradation_path") or []),
        guardrail_events=list(state.get("guardrail_events") or []),
    )

    result = _apply_output_guardrail(result)

    run_latency.observe(duration)
    run_steps.observe(len(steps))
    run_tool_calls.observe(runner.ctx.calls_made)
    runs_finished.labels(status=result.status.value).inc()
    if result.routing:
        routing_decisions.labels(queue=result.routing.queue.value).inc()
    log.info(
        "run_finished",
        run_id=run_id,
        status=result.status.value,
        queue=result.routing.queue.value if result.routing else None,
        position=result.coverage.position.value if result.coverage else None,
        verdict=result.verification.verdict,
        citations=len(result.citations),
        steps=len(steps),
        tool_calls=runner.ctx.calls_made,
        tokens=cost.total_tokens,
        cost_usd=cost.cost_usd,
        duration_seconds=duration,
        degraded=result.degraded,
    )
    return result


def _apply_output_guardrail(result: TriageResult) -> TriageResult:
    """L4. The last thing that happens before the caller sees anything."""
    surfaces = []
    if result.coverage:
        surfaces += [result.coverage.summary, result.coverage.reasoning]
    if result.routing:
        surfaces.append(result.routing.reason)

    findings = []
    for text in surfaces:
        check = check_output(text or "")
        findings.extend(f.to_dict() for f in check.findings)
        if not check.allowed:
            log.error(
                "unauthorised_output_blocked",
                run_id=result.run_id,
                patterns=[f.pattern for f in check.findings],
            )
            result.guardrail_events.extend(findings)
            result.status = RunStatus.escalated
            result.escalation = EscalationNotice(
                required=True,
                reason=EscalationReason.guardrail_block,
                explanation=(
                    "the drafted response contained a determination this service is "
                    "not authorised to make; it has been withheld and the claim "
                    "referred to a human handler"
                ),
            )
            result.coverage = CoverageDetermination(
                position=CoveragePosition.undetermined,
                summary="Withheld by the output guardrail.",
                reasoning=(
                    "The service is advisory. The drafted text asserted an approval, "
                    "rejection or settlement amount, which only a claims handler may "
                    "do, so it was not returned."
                ),
                confidence=0.0,
            )
            result.routing = RoutingDecision(
                queue=RoutingQueue.human_escalation,
                reason="output guardrail blocked the drafted determination",
                priority="high",
                target_first_touch_hours=4,
                triggered_rules=[f["pattern"] for f in findings],
            )
            return result
    if findings:
        result.guardrail_events.extend(findings)
    return result


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
