"""The LangGraph reasoning layer (A1, A2, A3).

Topology -- four agents, one shared state object, explicit hand-offs
--------------------------------------------------------------------

    START
      |
      v
  [Intake Analyst]  classify, extract entities, propose the first queries
      |  hand-off: classification + retrieval plan
      v
  [Coverage Analyst]  ground_search -> plan_modifiers -> targeted_search -> analyse
      |  hand-off: draft determination + the passages it was written from
      v
  [Verifier]  per-claim entailment against those passages, plus citation resolution
      |  verified ------------------> [Routing Officer] -> END
      |  unsupported & budget left -> back to Coverage Analyst with a *new strategy*
      |  unsupported & no budget ---> [Escalation] -> END
      v
  [Routing Officer]  choose a queue, floored by the deterministic signals
      |
     END

These are four agents rather than four prompts because each owns a distinct
part of the state, each can fail independently, and the Verifier in particular
is adversarial to the Coverage Analyst by design -- it is given the passages and
the draft, not the Analyst's reasoning, and it is the only node that can send
work back. What happens when one fails mid-run is in docs/agent-topology.md and
is implemented in `_run_node`.

Why the loop is not a fixed pipeline (A1)
-----------------------------------------
Two nodes decide whether to retrieve at all, and what to retrieve.
`intake` emits the queries it wants run; `plan_modifiers` decides whether any
provision might displace the grant and names what to fetch. If it answers "no
modifiers possible", no second retrieval happens. If it names a clause, the
graph resolves that clause by name rather than by similarity. Retrieval is
something the agent asks for, not a stage it passes through.

Why re-searching uses a different strategy each time (A3)
---------------------------------------------------------
Retrying a failed search with the same strategy is how a system convinces
itself. The escalation ladder is in `SEARCH_STRATEGIES`: analyst-authored
queries, then a role-filtered sweep for anything that restricts or excludes,
then neighbour expansion around what has already been cited plus a lexical-only
pass over the policy's terms of art. Each one looks for different things.

Bounds (A1)
-----------
Steps, tool calls and wall-clock are all bounded, from configuration. A run
that hits a bound does not return a confident answer; it escalates, and says
which bound it hit.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from ..config import get_settings
from ..contracts.models import (
    Citation,
    Classification,
    ClaimType,
    CostBreakdown,
    CoverageDetermination,
    CoveragePosition,
    EscalationNotice,
    EscalationReason,
    RoutingDecision,
    RoutingQueue,
    StepRecord,
    VerificationReport,
)
from ..guardrails import check_output
from ..llm import CircuitOpen, LLMClient, ProviderError, get_client, parse_json_object
from ..logging_setup import get_logger
from ..prompts import get_registry
from ..retrieval.index import SearchHit, get_index
from ..telemetry import (
    agent_step_latency,
    agent_step_outcome,
    agent_task_success,
    span,
    verification_outcome,
)
from . import signals as signal_rules
from .tools import (
    ToolBudgetExceeded,
    ToolContext,
    ToolTimeout,
    clause_neighbours,
    corpus_search,
    find_named_clause,
    get_claim_field,
)

log = get_logger("agent")

INTAKE_ANALYST = "intake_analyst"
COVERAGE_ANALYST = "coverage_analyst"
VERIFIER = "verifier"
ROUTING_OFFICER = "routing_officer"

#: The strategy ladder. Index = attempt number.
SEARCH_STRATEGIES = ["analyst_queries", "restriction_sweep", "neighbour_and_lexical"]

#: Terms of art the lexical pass falls back on. These are the words the policy
#: uses for the things a customer never says.
POLICY_TERMS_OF_ART = [
    "limitations as to use hire or reward carriage of goods",
    "general exceptions the company shall not be liable",
    "driver's clause effective driving licence disqualified",
    "conditions precedent to liability notice of claim",
]


def _append(left: list, right: list) -> list:
    return (left or []) + (right or [])


class TriageState(TypedDict, total=False):
    # identity
    run_id: str
    correlation_id: str
    claim: dict[str, Any]
    warnings: list[dict[str, Any]]

    # agent outputs
    classification: dict[str, Any]
    plan: dict[str, Any]
    draft: dict[str, Any]
    verification: dict[str, Any]
    routing: dict[str, Any]

    # bookkeeping
    steps: Annotated[list[dict[str, Any]], _append]
    prompt_versions: dict[str, str]
    cost_per_step: dict[str, float]
    tokens_per_step: dict[str, int]
    tokens: dict[str, int]
    attempt: int
    strategy: str
    escalation: dict[str, Any] | None
    degraded: bool
    degradation_path: Annotated[list[str], _append]
    guardrail_events: Annotated[list[dict[str, Any]], _append]
    terminal: str | None


class AgentRunner:
    """Owns one run. Not shared between runs: it holds the tool budget."""

    def __init__(
        self,
        *,
        run_id: str,
        correlation_id: str,
        claim: dict[str, Any],
        warnings: list[dict[str, Any]] | None = None,
        client: LLMClient | None = None,
        index=None,
        prompt_set: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.run_id = run_id
        self.correlation_id = correlation_id
        self.claim = claim
        self.warnings = warnings or []
        self.client = client or get_client()
        self.index = index or get_index()
        self.prompt_set = prompt_set or self.settings.prompt_set
        self.registry = get_registry()
        self.started_monotonic = time.monotonic()
        self.deadline = self.started_monotonic + self.settings.agent_wall_clock_seconds
        self.ctx = ToolContext(
            claim=claim,
            index=self.index,
            max_calls=self.settings.agent_max_tool_calls,
            deadline=self.deadline,
        )
        self.step_index = 0
        self.graph = _build_graph(self)

    # -- infrastructure -------------------------------------------------------

    def _budget_exhausted(self, state: TriageState) -> str | None:
        if len(state.get("steps") or []) >= self.settings.agent_max_steps:
            return "max_steps"
        if self.ctx.calls_made >= self.settings.agent_max_tool_calls:
            return "max_tool_calls"
        if time.monotonic() >= self.deadline:
            return "wall_clock"
        return None

    def _record(
        self,
        *,
        agent: str,
        step: str,
        started: float,
        outcome: str,
        prompt_version: str | None = None,
        usage: Any = None,
        detail: dict[str, Any] | None = None,
        tool: str | None = None,
        tool_query: str | None = None,
        clause_ids: list[str] | None = None,
        top_score: float | None = None,
    ) -> dict[str, Any]:
        duration_ms = (time.perf_counter() - started) * 1000
        self.step_index += 1
        agent_step_latency.labels(agent=agent, step=step).observe(duration_ms / 1000)
        agent_step_outcome.labels(agent=agent, step=step, outcome=outcome).inc()
        agent_task_success.labels(
            agent=agent, result="success" if outcome in {"ok", "skipped"} else "failure"
        ).inc()
        record = StepRecord(
            index=self.step_index,
            agent=agent,
            step=step,
            started_at=datetime.now(timezone.utc),
            duration_ms=round(duration_ms, 2),
            outcome=outcome,
            prompt_version=prompt_version,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            cost_usd=round(getattr(usage, "cost_usd", 0.0), 8) if usage else 0.0,
            tool=tool,
            tool_query=tool_query,
            retrieved_clause_ids=clause_ids or [],
            top_score=top_score,
            detail=detail or {},
        ).model_dump(mode="json")
        log.info(
            "agent_step",
            agent=agent,
            step=step,
            outcome=outcome,
            duration_ms=record["duration_ms"],
            prompt_version=prompt_version,
            tokens=record["prompt_tokens"] + record["completion_tokens"],
            cost_usd=record["cost_usd"],
        )
        return record

    def _call_model(self, step: str, **variables: Any):
        prompt = self.registry.get(step, self.prompt_set)
        messages = prompt.render(**variables)
        completion = self.client.complete(messages, step=step, json_mode=True)
        return prompt, completion

    # -- context rendering ----------------------------------------------------

    def _format_context(self, hits: list[SearchHit], limit: int = 10) -> str:
        if not hits:
            return "(no passages retrieved)"
        ordered = sorted(hits, key=lambda h: -h.score)[:limit]
        blocks = []
        for hit in ordered:
            blocks.append(
                f"[{hit.clause_id}] ({hit.role}, {hit.document_title}, "
                f"{hit.section_path}, page {hit.page})\n{hit.text}"
            )
        return "\n\n".join(blocks)

    def _claim_summary(self) -> str:
        claim = self.claim
        incident = claim.get("incident") or {}
        vehicle = claim.get("vehicle") or {}
        driver = claim.get("driver") or {}
        return json.dumps(
            {
                "claim_reference": claim.get("claim_reference"),
                "policy_number": claim.get("policy_number"),
                "date_of_loss": claim.get("date_of_loss"),
                "vehicle": {
                    "make": vehicle.get("make"),
                    "model": vehicle.get("model") or vehicle.get("model_name"),
                    "year": vehicle.get("year_of_manufacture"),
                    "idv_inr": vehicle.get("insured_declared_value_inr"),
                },
                "driver": {
                    "relationship": driver.get("relationship_to_insured"),
                    "licence_valid": driver.get("licence_valid_on_date_of_loss"),
                    "learner_only": driver.get("holds_learner_licence_only"),
                },
                "incident": {
                    "declared_use_at_time_of_loss": incident.get("vehicle_use_at_time_of_loss"),
                    "vehicle_drivable": incident.get("vehicle_drivable"),
                    "third_party_involved": incident.get("third_party_involved"),
                    "injuries": incident.get("third_party_injuries_reported")
                    or incident.get("occupant_injuries_reported"),
                    "vehicle_stolen": incident.get("vehicle_stolen"),
                    "estimated_repair_cost_inr": incident.get("estimated_repair_cost_inr"),
                    "fir_number": incident.get("fir_number"),
                    "description": incident.get("description"),
                },
            },
            indent=2,
            default=str,
        )

    # -- execution ------------------------------------------------------------

    def run(self) -> TriageState:
        initial: TriageState = {
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "claim": self.claim,
            "warnings": self.warnings,
            "steps": [],
            "prompt_versions": {},
            "cost_per_step": {},
            "tokens_per_step": {},
            "tokens": {"prompt": 0, "completion": 0},
            "attempt": 0,
            "strategy": SEARCH_STRATEGIES[0],
            "escalation": None,
            "degraded": False,
            "degradation_path": [],
            "guardrail_events": [],
            "terminal": None,
        }
        with span(
            "agent.run",
            **{"run.id": self.run_id, "run.correlation_id": self.correlation_id},
        ):
            final = self.graph.invoke(
                initial,
                config={"recursion_limit": max(12, self.settings.agent_max_steps * 3)},
            )
        final["guardrail_events"] = (final.get("guardrail_events") or []) + list(
            self.ctx.guardrail_findings
        )
        return final


# -----------------------------------------------------------------------------
# Nodes
# -----------------------------------------------------------------------------


def _escalate(
    runner: AgentRunner,
    state: TriageState,
    reason: EscalationReason,
    explanation: str,
    *,
    degraded: bool = False,
) -> TriageState:
    log.warning(
        "run_escalated", reason=reason.value, explanation=explanation, run_id=runner.run_id
    )
    verification_outcome.labels(outcome="escalate").inc()
    return {
        "escalation": EscalationNotice(
            required=True, reason=reason, explanation=explanation
        ).model_dump(mode="json"),
        "terminal": "escalated",
        "degraded": degraded or state.get("degraded", False),
    }


def _accumulate(state: TriageState, step: str, completion) -> dict[str, Any]:
    """Attribute one model call's tokens and spend to the step that made it (L5).

    Both are tracked per step. Spend alone is not enough: on a free tier every
    per-step figure is 0.00 and the largest cost driver becomes unanswerable,
    which is the question L5 actually asks.
    """
    costs = dict(state.get("cost_per_step") or {})
    costs[step] = round(costs.get(step, 0.0) + completion.usage.cost_usd, 8)
    tokens = dict(state.get("tokens") or {"prompt": 0, "completion": 0})
    tokens["prompt"] += completion.usage.prompt_tokens
    tokens["completion"] += completion.usage.completion_tokens
    per_step = dict(state.get("tokens_per_step") or {})
    per_step[step] = per_step.get(step, 0) + completion.usage.total_tokens
    return {"cost_per_step": costs, "tokens": tokens, "tokens_per_step": per_step}


def _node_intake(runner: AgentRunner, state: TriageState) -> TriageState:
    started = time.perf_counter()
    incident = (state["claim"].get("incident") or {})
    try:
        prompt, completion = runner._call_model(
            "intake_classification",
            structured_fields=runner._claim_summary(),
            description=incident.get("description", ""),
            warnings=json.dumps(state.get("warnings") or [], default=str),
        )
        payload = parse_json_object(completion.text)
    except CircuitOpen as exc:
        step = runner._record(
            agent=INTAKE_ANALYST, step="intake_classification", started=started,
            outcome="circuit_open", detail={"error": str(exc)},
        )
        return {
            "steps": [step],
            **_escalate(
                runner, state, EscalationReason.provider_unavailable,
                f"model provider unavailable at intake: {exc}", degraded=True,
            ),
            "degradation_path": ["model_unavailable_at_intake"],
        }
    except (ProviderError, ValueError) as exc:
        step = runner._record(
            agent=INTAKE_ANALYST, step="intake_classification", started=started,
            outcome="failed", detail={"error": str(exc)[:300]},
        )
        # Fallback path: classify from the structured fields alone and carry on
        # degraded, rather than abandoning a claim because one call failed.
        payload = _classify_without_model(state["claim"])
        return {
            "steps": [step],
            "classification": payload,
            "degraded": True,
            "degradation_path": ["intake_classified_from_structured_fields_only"],
        }

    versions = dict(state.get("prompt_versions") or {})
    versions["intake_classification"] = prompt.version_id
    step = runner._record(
        agent=INTAKE_ANALYST, step="intake_classification", started=started, outcome="ok",
        prompt_version=prompt.version_id, usage=completion.usage,
        detail={
            "claim_type": payload.get("claim_type"),
            "confidence": payload.get("confidence"),
            "queries_proposed": payload.get("retrieval_queries") or [],
        },
    )
    return {
        "steps": [step],
        "classification": payload,
        "prompt_versions": versions,
        **_accumulate(state, "intake_classification", completion),
    }


def _classify_without_model(claim: dict[str, Any]) -> dict[str, Any]:
    """Degradation path: the structured fields alone can carry the classification."""
    incident = claim.get("incident") or {}
    vehicle = claim.get("vehicle") or {}
    estimate = float(incident.get("estimated_repair_cost_inr") or 0)
    idv = float(vehicle.get("insured_declared_value_inr") or 0)
    if incident.get("vehicle_stolen"):
        claim_type = "theft"
    elif idv and estimate >= idv * signal_rules.TOTAL_LOSS_RATIO:
        claim_type = "total_loss"
    elif incident.get("third_party_involved"):
        claim_type = "third_party"
    else:
        claim_type = "own_damage"
    return {
        "claim_type": claim_type,
        "confidence": 0.5,
        "secondary_types": [],
        "entities": {"derived_without_model": True},
        "rationale": "Derived from the validated structured fields; the model was unavailable.",
        "retrieval_queries": [
            "loss of or damage to the vehicle insured accidental external means",
            "general exceptions applicable to all sections of the policy",
        ],
    }


def _node_ground_search(runner: AgentRunner, state: TriageState) -> TriageState:
    """Retrieve the provision that appears to apply (design step 1)."""
    started = time.perf_counter()
    classification = state.get("classification") or {}
    queries = [q for q in (classification.get("retrieval_queries") or []) if isinstance(q, str)]
    if not queries:
        queries = ["loss of or damage to the vehicle insured accidental external means"]

    hits: list[SearchHit] = []
    try:
        for query in queries[:2]:
            hits.extend(corpus_search(runner.ctx, query))
    except (ToolBudgetExceeded, ToolTimeout) as exc:
        step = runner._record(
            agent=COVERAGE_ANALYST, step="ground_search", started=started,
            outcome="budget_exceeded", detail={"error": str(exc)},
        )
        return {
            "steps": [step],
            **_escalate(runner, state, EscalationReason.loop_budget_exhausted, str(exc)),
        }

    best = max((h.score for h in hits), default=0.0)
    step = runner._record(
        agent=COVERAGE_ANALYST, step="ground_search", started=started,
        outcome="ok" if hits else "empty", tool="corpus_search",
        tool_query=" | ".join(queries[:2]),
        clause_ids=[h.clause_id for h in hits], top_score=round(best, 4),
    )

    if not hits or best < runner.settings.escalation_min_retrieval_score:
        return {
            "steps": [step],
            **_escalate(
                runner, state, EscalationReason.weak_retrieval,
                f"best retrieval score {best:.3f} is below the "
                f"{runner.settings.escalation_min_retrieval_score} floor; the corpus "
                "does not appear to address this claim",
            ),
        }
    return {"steps": [step]}


def _node_plan_modifiers(runner: AgentRunner, state: TriageState) -> TriageState:
    """Decide whether another provision modifies the grant (design step 2).

    This node is the reason this is an agent. It is allowed to answer "nothing
    else applies" and end the search, and it is allowed to name a clause it has
    not seen and ask for it by name.
    """
    started = time.perf_counter()
    try:
        prompt, completion = runner._call_model(
            "query_formulation",
            claim_summary=runner._claim_summary(),
            classification=json.dumps(state.get("classification") or {}, default=str),
            context=runner._format_context(list(runner.ctx.retrieved.values())),
            prior_queries=json.dumps(runner.ctx.queries_issued, default=str),
        )
        plan = parse_json_object(completion.text)
    except CircuitOpen as exc:
        step = runner._record(
            agent=COVERAGE_ANALYST, step="plan_modifiers", started=started,
            outcome="circuit_open", detail={"error": str(exc)},
        )
        return {
            "steps": [step],
            **_escalate(
                runner, state, EscalationReason.provider_unavailable,
                f"model provider unavailable while planning: {exc}", degraded=True,
            ),
            "degradation_path": ["model_unavailable_at_planning"],
        }
    except (ProviderError, ValueError) as exc:
        # Fallback: a fixed restriction sweep is strictly better than skipping
        # the modifier search altogether, which is the failure in section 4.1.
        step = runner._record(
            agent=COVERAGE_ANALYST, step="plan_modifiers", started=started,
            outcome="failed_using_default_plan", detail={"error": str(exc)[:300]},
        )
        return {
            "steps": [step],
            "plan": {
                "modifiers_possible": True,
                "named_clauses_to_fetch": ["Limitations as to Use", "General Exceptions"],
                "queries": POLICY_TERMS_OF_ART[:2],
                "reason": "default modifier sweep; the planner call failed",
            },
            "degraded": True,
            "degradation_path": ["planner_fallback_fixed_sweep"],
        }

    versions = dict(state.get("prompt_versions") or {})
    versions["query_formulation"] = prompt.version_id
    step = runner._record(
        agent=COVERAGE_ANALYST, step="plan_modifiers", started=started, outcome="ok",
        prompt_version=prompt.version_id, usage=completion.usage,
        detail={
            "modifiers_possible": plan.get("modifiers_possible"),
            "named_clauses": plan.get("named_clauses_to_fetch") or [],
            "queries": plan.get("queries") or [],
            "checked": plan.get("checked") or {},
        },
    )
    return {
        "steps": [step],
        "plan": plan,
        "prompt_versions": versions,
        **_accumulate(state, "query_formulation", completion),
    }


def _node_targeted_search(runner: AgentRunner, state: TriageState) -> TriageState:
    """Search again, with a query derived from what has been read (step 3).

    The strategy escalates on each attempt -- see SEARCH_STRATEGIES.
    """
    started = time.perf_counter()
    strategy = state.get("strategy", SEARCH_STRATEGIES[0])
    plan = state.get("plan") or {}
    found: list[SearchHit] = []
    actions: list[str] = []

    try:
        if strategy == "analyst_queries":
            for name in (plan.get("named_clauses_to_fetch") or [])[:2]:
                if isinstance(name, str) and name.strip():
                    found.extend(find_named_clause(runner.ctx, name.strip()))
                    actions.append(f"find_named_clause({name})")
            for query in (plan.get("queries") or [])[:2]:
                if isinstance(query, str) and query.strip():
                    found.extend(corpus_search(runner.ctx, query.strip()))
                    actions.append(f"corpus_search({query[:60]})")

        elif strategy == "restriction_sweep":
            # Ask the corpus directly for anything that could take cover away,
            # in the policy's own vocabulary rather than the customer's.
            declared_use = get_claim_field(runner.ctx, "incident.vehicle_use_at_time_of_loss")
            actions.append(f"get_claim_field(incident.vehicle_use_at_time_of_loss)={declared_use}")
            sweep = f"use of the vehicle {str(declared_use).replace('_', ' ')} permitted purposes"
            found.extend(
                corpus_search(runner.ctx, sweep, role=["restriction", "exclusion"], top_k=6)
            )
            actions.append(f"corpus_search[restriction,exclusion]({sweep[:60]})")
            for name in ("Limitations as to Use", "General Exceptions", "Driver's Clause"):
                if runner.ctx.remaining_calls <= 1:
                    break
                found.extend(find_named_clause(runner.ctx, name))
                actions.append(f"find_named_clause({name})")

        else:  # neighbour_and_lexical
            cited = [
                c.get("clause_id")
                for c in ((state.get("draft") or {}).get("citations") or [])
                if isinstance(c, dict) and c.get("clause_id")
            ]
            for clause_id in cited[:2]:
                found.extend(clause_neighbours(runner.ctx, clause_id, window=1))
                actions.append(f"clause_neighbours({clause_id})")
            for term in POLICY_TERMS_OF_ART[:2]:
                if runner.ctx.remaining_calls <= 1:
                    break
                found.extend(corpus_search(runner.ctx, term, strategy="lexical", top_k=4))
                actions.append(f"corpus_search[lexical]({term[:50]})")

    except (ToolBudgetExceeded, ToolTimeout) as exc:
        step = runner._record(
            agent=COVERAGE_ANALYST, step="targeted_search", started=started,
            outcome="budget_exceeded", detail={"strategy": strategy, "error": str(exc), "actions": actions},
        )
        # Not fatal: we may already hold enough to answer. Carry on to analysis
        # with what we have and let the Verifier decide.
        return {"steps": [step], "degraded": True, "degradation_path": [f"tool_budget_hit_in_{strategy}"]}

    step = runner._record(
        agent=COVERAGE_ANALYST, step="targeted_search", started=started,
        outcome="ok" if found else "empty", tool="multiple",
        tool_query=" ; ".join(actions)[:500],
        clause_ids=[h.clause_id for h in found],
        top_score=round(max((h.score for h in found), default=0.0), 4),
        detail={"strategy": strategy, "actions": actions},
    )
    return {"steps": [step]}


def _node_analyse(runner: AgentRunner, state: TriageState) -> TriageState:
    """Produce the draft coverage position from the retrieved passages."""
    started = time.perf_counter()
    passages = list(runner.ctx.retrieved.values())
    flags = {
        "validation_warnings": state.get("warnings") or [],
        "declared_use": (state["claim"].get("incident") or {}).get("vehicle_use_at_time_of_loss"),
        "roles_retrieved": sorted({h.role for h in passages}),
        "attempt": state.get("attempt", 0),
    }
    try:
        prompt, completion = runner._call_model(
            "coverage_analysis",
            claim_summary=runner._claim_summary(),
            classification=json.dumps(state.get("classification") or {}, default=str),
            context=runner._format_context(passages, limit=12),
            flags=json.dumps(flags, default=str),
        )
        draft = parse_json_object(completion.text)
    except CircuitOpen as exc:
        step = runner._record(
            agent=COVERAGE_ANALYST, step="coverage_analysis", started=started,
            outcome="circuit_open", detail={"error": str(exc)},
        )
        return {
            "steps": [step],
            **_escalate(
                runner, state, EscalationReason.provider_unavailable,
                f"model provider unavailable; the retrieved passages are attached for "
                f"a human handler ({exc})",
                degraded=True,
            ),
            "degradation_path": ["model_unavailable_at_analysis_returning_evidence_only"],
        }
    except (ProviderError, ValueError) as exc:
        step = runner._record(
            agent=COVERAGE_ANALYST, step="coverage_analysis", started=started,
            outcome="failed", detail={"error": str(exc)[:300]},
        )
        return {
            "steps": [step],
            **_escalate(
                runner, state, EscalationReason.verification_failed,
                f"the coverage analysis call failed and could not be parsed: {exc}",
                degraded=True,
            ),
        }

    versions = dict(state.get("prompt_versions") or {})
    versions["coverage_analysis"] = prompt.version_id
    step = runner._record(
        agent=COVERAGE_ANALYST, step="coverage_analysis", started=started, outcome="ok",
        prompt_version=prompt.version_id, usage=completion.usage,
        clause_ids=[
            c.get("clause_id") for c in (draft.get("citations") or []) if isinstance(c, dict)
        ],
        detail={
            "position": draft.get("position"),
            "confidence": draft.get("confidence"),
            "citations": len(draft.get("citations") or []),
        },
    )
    return {
        "steps": [step],
        "draft": draft,
        "prompt_versions": versions,
        **_accumulate(state, "coverage_analysis", completion),
    }


def _node_verify(runner: AgentRunner, state: TriageState) -> TriageState:
    """A3. Two checks, in order, and the cheap one runs first.

    1. Deterministic: does every quote resolve, character-for-character, to the
       passage it is attributed to? No model is needed to answer that, and a
       model should not be trusted to. A citation that does not resolve is a
       fabrication regardless of what any verifier says about it.
    2. Model: per-claim entailment of the determination against those passages.

    An answer that fails either check is never returned.
    """
    started = time.perf_counter()
    draft = state.get("draft") or {}
    raw_citations = [c for c in (draft.get("citations") or []) if isinstance(c, dict)]

    resolved, unresolved = [], []
    for citation in raw_citations:
        clause_id = str(citation.get("clause_id") or "")
        quote = str(citation.get("quote") or "")
        if clause_id and quote and runner.index.quote_resolves(clause_id, quote):
            resolved.append(citation)
        else:
            unresolved.append({"clause_id": clause_id, "quote": quote[:160]})

    citation_rate = len(resolved) / len(raw_citations) if raw_citations else 0.0

    model_verdict: dict[str, Any] = {}
    prompt_version = None
    usage = None
    try:
        prompt, completion = runner._call_model(
            "verification",
            determination=json.dumps(
                {k: v for k, v in draft.items() if k != "citations"}
                | {"citations": resolved},
                indent=2,
                default=str,
            ),
            context=runner._format_context(list(runner.ctx.retrieved.values()), limit=12),
        )
        model_verdict = parse_json_object(completion.text)
        prompt_version = prompt.version_id
        usage = completion.usage
    except (CircuitOpen, ProviderError, ValueError) as exc:
        # Degradation: fall back to the deterministic half alone. It is
        # stricter about citations and blind to reasoning, so we require a
        # perfect citation rate before accepting an answer on it.
        model_verdict = {
            "verdict": "verified" if raw_citations and citation_rate >= 1.0 else "unsupported",
            "support_score": citation_rate,
            "unsupported_statements": [] if citation_rate >= 1.0 else ["model verifier unavailable"],
            "notes": f"deterministic citation check only: {exc}",
        }
        state = {**state, "degraded": True}

    support = float(model_verdict.get("support_score") or 0.0)
    combined = round(0.5 * support + 0.5 * citation_rate, 4)
    verdict = (
        "verified"
        if (
            model_verdict.get("verdict") == "verified"
            and not unresolved
            and combined >= runner.settings.verifier_min_support
        )
        else "unsupported"
    )

    report = {
        "verdict": verdict,
        "support_score": combined,
        "model_support_score": support,
        "citation_resolution_rate": round(citation_rate, 4),
        "citations_checked": len(raw_citations),
        "citations_resolved": len(resolved),
        "unresolved_citations": unresolved,
        "unsupported_statements": model_verdict.get("unsupported_statements") or [],
        "clauses_in_conflict": model_verdict.get("clauses_in_conflict") or [],
        "attempts": state.get("attempt", 0) + 1,
        "notes": model_verdict.get("notes", ""),
        "resolved_citations": resolved,
    }
    verification_outcome.labels(outcome=verdict).inc()

    versions = dict(state.get("prompt_versions") or {})
    if prompt_version:
        versions["verification"] = prompt_version
    step = runner._record(
        agent=VERIFIER, step="verification", started=started,
        outcome=verdict, prompt_version=prompt_version, usage=usage,
        detail={
            "support_score": combined,
            "citations_resolved": f"{len(resolved)}/{len(raw_citations)}",
            "unresolved": unresolved[:3],
        },
    )
    patch: dict[str, Any] = {
        "steps": [step],
        "verification": report,
        "prompt_versions": versions,
        "attempt": state.get("attempt", 0) + 1,
    }
    if usage is not None:
        patch.update(_accumulate(state, "verification", completion))
    if state.get("degraded"):
        patch["degraded"] = True

    # Persist the escalation notice here: the conditional edge that reads it
    # cannot write state.
    _, notice = decide_after_verification(runner, {**state, **patch})
    if notice is not None:
        patch["escalation"] = notice
    return patch


def _node_replan(runner: AgentRunner, state: TriageState) -> TriageState:
    """Choose a *different* search strategy and go round again (design step 5)."""
    started = time.perf_counter()
    attempt = state.get("attempt", 1)
    strategy = SEARCH_STRATEGIES[min(attempt, len(SEARCH_STRATEGIES) - 1)]
    step = runner._record(
        agent=COVERAGE_ANALYST, step="replan", started=started, outcome="ok",
        detail={
            "attempt": attempt,
            "next_strategy": strategy,
            "because": (state.get("verification") or {}).get("notes", "")[:200],
            "unresolved_citations": (state.get("verification") or {}).get("unresolved_citations", [])[:3],
        },
    )
    verification_outcome.labels(outcome="retry").inc()
    log.info("retrying_with_new_strategy", attempt=attempt, strategy=strategy)
    return {"steps": [step], "strategy": strategy}


def _node_route(runner: AgentRunner, state: TriageState) -> TriageState:
    started = time.perf_counter()
    draft = state.get("draft") or {}
    position = draft.get("position")
    computed = signal_rules.compute(state["claim"], coverage_position=position)

    try:
        prompt, completion = runner._call_model(
            "routing",
            coverage=json.dumps(
                {k: draft.get(k) for k in ("position", "summary", "reasoning", "open_questions")},
                default=str,
            ),
            claim_summary=runner._claim_summary(),
            signals=json.dumps(computed.to_dict(), indent=2, default=str),
        )
        choice = parse_json_object(completion.text)
        prompt_version, usage = prompt.version_id, completion.usage
    except (CircuitOpen, ProviderError, ValueError) as exc:
        # Degradation: route on the deterministic signals alone. This is a
        # complete, defensible routing decision -- it is simply more cautious
        # than the model would have been.
        choice = {
            "queue": computed.floor.value,
            "reason": "Routed on deterministic signals alone; the routing model call failed.",
            "priority": "high" if computed.investigation else "normal",
            "target_first_touch_hours": 8 if computed.investigation else 24,
            "triggered_rules": list(computed.investigation) + list(computed.surveyor),
        }
        prompt_version, usage = None, None
        state = {**state, "degraded": True}
        log.warning("routing_degraded_to_rules", error=str(exc)[:200])

    try:
        chosen = RoutingQueue(choice.get("queue"))
    except ValueError:
        chosen = computed.floor
    final_queue, overrides = signal_rules.enforce(chosen, computed)

    decision = {
        "queue": final_queue.value,
        "reason": str(choice.get("reason") or "")[:800],
        "priority": choice.get("priority") if choice.get("priority") in {"low", "normal", "high", "urgent"} else "normal",
        "target_first_touch_hours": int(choice.get("target_first_touch_hours") or 24),
        "triggered_rules": sorted(
            set(list(choice.get("triggered_rules") or []) + list(computed.investigation) + list(computed.surveyor))
        ),
        "model_choice": chosen.value,
        "overridden_by_rules": overrides,
        "signals": computed.to_dict(),
    }
    if overrides:
        decision["reason"] = (
            f"{decision['reason']} Raised to {final_queue.value} by mandatory rules: "
            f"{', '.join(overrides)}."
        ).strip()

    versions = dict(state.get("prompt_versions") or {})
    if prompt_version:
        versions["routing"] = prompt_version
    step = runner._record(
        agent=ROUTING_OFFICER, step="routing", started=started, outcome="ok",
        prompt_version=prompt_version, usage=usage,
        detail={"queue": final_queue.value, "overrides": overrides},
    )
    patch: dict[str, Any] = {
        "steps": [step],
        "routing": decision,
        "prompt_versions": versions,
        "terminal": "completed",
    }
    if usage is not None:
        patch.update(_accumulate(state, "routing", completion))
    if state.get("degraded"):
        patch["degraded"] = True
    return patch


def _node_escalation(runner: AgentRunner, state: TriageState) -> TriageState:
    """Terminal node for every escalation path, so routing is always populated."""
    started = time.perf_counter()
    computed = signal_rules.compute(
        state["claim"], coverage_position=(state.get("draft") or {}).get("position")
    )
    escalation = state.get("escalation") or EscalationNotice(
        required=True,
        reason=EscalationReason.verification_failed,
        explanation="the answer could not be verified against the retrieved passages",
    ).model_dump(mode="json")
    decision = {
        "queue": RoutingQueue.human_escalation.value,
        "reason": escalation.get("explanation", "escalated to a human claims handler"),
        "priority": "high",
        "target_first_touch_hours": 4,
        "triggered_rules": ["escalation:" + str(escalation.get("reason"))],
        "signals": computed.to_dict(),
    }
    step = runner._record(
        agent=VERIFIER, step="escalation", started=started, outcome="escalated",
        detail={"reason": escalation.get("reason")},
    )
    return {
        "steps": [step],
        "escalation": escalation,
        "routing": decision,
        "terminal": "escalated",
    }


# -----------------------------------------------------------------------------
# Edges
# -----------------------------------------------------------------------------


def _after_intake(runner: AgentRunner, state: TriageState) -> Literal["ground_search", "escalate"]:
    return "escalate" if state.get("terminal") == "escalated" else "ground_search"


def _after_ground(runner: AgentRunner, state: TriageState) -> Literal["plan_modifiers", "escalate"]:
    return "escalate" if state.get("terminal") == "escalated" else "plan_modifiers"


def _after_plan(runner: AgentRunner, state: TriageState) -> Literal["targeted_search", "analyse", "escalate"]:
    if state.get("terminal") == "escalated":
        return "escalate"
    plan = state.get("plan") or {}
    wants_more = bool(plan.get("modifiers_possible")) and bool(
        (plan.get("queries") or []) or (plan.get("named_clauses_to_fetch") or [])
    )
    # The agent is permitted to decide no further retrieval is needed. That is
    # the difference between a tool it chooses to call and a fixed stage.
    return "targeted_search" if wants_more else "analyse"


def _after_targeted(runner: AgentRunner, state: TriageState) -> Literal["analyse", "escalate"]:
    return "escalate" if state.get("terminal") == "escalated" else "analyse"


def _after_analyse(runner: AgentRunner, state: TriageState) -> Literal["verify", "escalate"]:
    return "escalate" if state.get("terminal") == "escalated" else "verify"


def decide_after_verification(
    runner: AgentRunner, state: TriageState
) -> tuple[str, dict[str, Any] | None]:
    """Where a verification result sends the run, and why.

    Returns the next node and the escalation notice to record, if any. This is
    a plain function rather than logic inside the conditional edge because a
    conditional edge in LangGraph must only *read* the state -- anything it
    writes to the dict is discarded. The notice therefore has to be produced by
    the verify node, which is the only place that can persist it.
    """
    report = state.get("verification") or {}
    if report.get("verdict") == "verified":
        return "route", None
    if report.get("clauses_in_conflict"):
        return "escalate", EscalationNotice(
            required=True,
            reason=EscalationReason.conflicting_clauses,
            explanation=(
                "the retrieved clauses conflict with one another and the conflict "
                "cannot be resolved from the corpus"
            ),
        ).model_dump(mode="json")

    attempt = int(report.get("attempts") or state.get("attempt", 1))
    bound = runner._budget_exhausted(state)
    if attempt < len(SEARCH_STRATEGIES) and bound is None:
        return "replan", None

    unsupported = "; ".join(str(s) for s in (report.get("unsupported_statements") or [])[:3])
    unresolved = report.get("unresolved_citations") or []
    detail = unsupported or (
        f"{len(unresolved)} citation(s) did not resolve to retrieved text"
        if unresolved
        else "no supporting passage was found"
    )
    return "escalate", EscalationNotice(
        required=True,
        reason=(
            EscalationReason.loop_budget_exhausted if bound else EscalationReason.verification_failed
        ),
        explanation=(
            f"the determination could not be verified after {attempt} attempt(s)"
            + (f"; stopped on {bound}" if bound else "")
            + f". {detail}"
        ),
    ).model_dump(mode="json")


def _after_verify(runner: AgentRunner, state: TriageState) -> Literal["route", "replan", "escalate"]:
    destination, _ = decide_after_verification(runner, state)
    return destination  # type: ignore[return-value]


def _build_graph(runner: AgentRunner):
    graph = StateGraph(TriageState)
    graph.add_node("intake", lambda s: _node_intake(runner, s))
    graph.add_node("ground_search", lambda s: _node_ground_search(runner, s))
    graph.add_node("plan_modifiers", lambda s: _node_plan_modifiers(runner, s))
    graph.add_node("targeted_search", lambda s: _node_targeted_search(runner, s))
    graph.add_node("analyse", lambda s: _node_analyse(runner, s))
    graph.add_node("verify", lambda s: _node_verify(runner, s))
    graph.add_node("replan", lambda s: _node_replan(runner, s))
    graph.add_node("route", lambda s: _node_route(runner, s))
    graph.add_node("escalate", lambda s: _node_escalation(runner, s))

    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", lambda s: _after_intake(runner, s))
    graph.add_conditional_edges("ground_search", lambda s: _after_ground(runner, s))
    graph.add_conditional_edges("plan_modifiers", lambda s: _after_plan(runner, s))
    graph.add_conditional_edges("targeted_search", lambda s: _after_targeted(runner, s))
    graph.add_conditional_edges("analyse", lambda s: _after_analyse(runner, s))
    graph.add_conditional_edges("verify", lambda s: _after_verify(runner, s))
    graph.add_edge("replan", "targeted_search")
    graph.add_edge("route", END)
    graph.add_edge("escalate", END)
    return graph.compile()
