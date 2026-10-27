"""The agent graph (A1, A2, A3) and the result contract.

These run against the in-process stub provider and the synthetic corpus, so
they assert *structural* properties rather than answer quality: that the loop is
bounded, that the hand-offs happen in order, that retrieval is something the
agent chooses, that an unverifiable answer is never returned, and that the
degradation paths produce a usable result instead of an exception.

Answer quality is measured by the evaluation set against a real provider.
"""

from __future__ import annotations

import copy
import time

import pytest

from claimtriage.agents.graph import SEARCH_STRATEGIES
from claimtriage.agents.runner import execute_run, run_cache_key
from claimtriage.contracts.models import RoutingQueue, RunStatus
from claimtriage.llm import LLMClient, ProviderError, Usage
from claimtriage.llm.base import CircuitOpen
from claimtriage.validation import validate_submission


def _run(fnol: dict, **kwargs):
    outcome = validate_submission(fnol, correlation_id="cid-agent", client_id="pytest")
    assert outcome.ok, [i.model_dump() for i in outcome.errors]
    return execute_run(
        run_id=f"run-test-{int(time.time() * 1000) % 10**9}",
        correlation_id="cid-agent",
        claim=outcome.fnol.model_dump(mode="json"),
        warnings=[w.model_dump(mode="json") for w in outcome.warnings],
        use_cache=False,
        **kwargs,
    )


# -----------------------------------------------------------------------------
# Happy path and the result contract
# -----------------------------------------------------------------------------


def test_ordinary_claim_completes(built_index, valid_fnol):
    result = _run(valid_fnol)
    assert result.status in {RunStatus.completed, RunStatus.escalated}
    assert result.routing is not None
    assert result.run_id
    assert result.correlation_id == "cid-agent"
    assert result.duration_seconds > 0
    assert result.advisory_notice.startswith("Advisory only")


def test_result_round_trips_through_its_own_contract(built_index, valid_fnol):
    from claimtriage.contracts.models import TriageResult

    result = _run(valid_fnol)
    payload = result.model_dump(mode="json")
    assert TriageResult.model_validate(payload) == result


def test_every_returned_citation_resolves(built_index, valid_fnol):
    """A3, as a property of the response object rather than a prompt instruction."""
    from claimtriage.retrieval.index import get_index

    index = get_index()
    for fnol in (valid_fnol, _delivery(valid_fnol)):
        result = _run(fnol)
        for citation in result.citations:
            assert citation.resolves
            assert index.quote_resolves(citation.clause_id, citation.quote), (
                f"{citation.clause_id} quote did not resolve"
            )


def test_completed_runs_are_always_verified(built_index, valid_fnol):
    """An unverified answer must not be returned. If verification failed the
    run escalates; it never comes back as `completed`."""
    for fnol in (valid_fnol, _delivery(valid_fnol)):
        result = _run(fnol)
        if result.status == RunStatus.completed:
            assert result.verification.verdict == "verified"
            assert not result.escalation.required


def test_escalation_always_has_a_routing_decision(built_index, valid_fnol):
    """An escalated run is still routed -- to a human. A claim that fell out of
    the pipeline with no queue is a claim nobody picks up."""
    result = _run(_out_of_corpus(valid_fnol))
    assert result.routing is not None
    if result.escalation.required:
        assert result.routing.queue == RoutingQueue.human_escalation
        assert result.escalation.reason is not None
        assert result.escalation.explanation


# -----------------------------------------------------------------------------
# Topology and hand-offs (A2)
# -----------------------------------------------------------------------------


def test_agents_run_in_order_with_distinct_responsibilities(built_index, valid_fnol):
    result = _run(valid_fnol)
    agents = [s.agent for s in result.steps]
    assert agents[0] == "intake_analyst"
    assert "coverage_analyst" in agents
    assert "verifier" in agents
    # The verifier never runs before the analyst has produced something to check.
    assert agents.index("coverage_analyst") < agents.index("verifier")
    assert len({s.agent for s in result.steps}) >= 3


def test_step_records_are_complete(built_index, valid_fnol):
    result = _run(valid_fnol)
    assert result.steps
    for i, step in enumerate(result.steps, start=1):
        assert step.index == i
        assert step.agent and step.step and step.outcome
        assert step.duration_ms >= 0
        assert step.started_at is not None


def test_prompt_versions_are_recorded_per_step(built_index, valid_fnol):
    """L2: every response records the prompt version that produced it."""
    result = _run(valid_fnol)
    assert result.prompt_versions
    for step, version in result.prompt_versions.items():
        assert version.startswith(f"{step}@v")
        assert "+" in version, "the version must carry a content hash"


def test_cost_is_attributed_per_step(built_index, valid_fnol):
    """L5: tokens and cost per step as well as per run."""
    result = _run(valid_fnol)
    assert result.cost.total_tokens > 0
    assert result.cost.prompt_tokens > 0
    assert result.cost.per_step
    assert result.cost.largest_driver in result.cost.per_step


# -----------------------------------------------------------------------------
# Retrieval is a tool the agent chooses (A1)
# -----------------------------------------------------------------------------


def test_tools_are_actually_called(built_index, valid_fnol):
    result = _run(valid_fnol)
    assert result.tool_calls > 0
    queries = [s.tool_query for s in result.steps if s.tool_query]
    assert queries, "no tool query was recorded in the trace"


def test_a_search_step_records_what_it_retrieved(built_index, valid_fnol):
    result = _run(valid_fnol)
    # Search steps carry a retrieval score. The analysis step also records
    # clause ids -- the ones it chose to cite -- but those came from the
    # analyst, not from a ranker, so there is no score to report for them.
    searches = [s for s in result.steps if s.step in {"ground_search", "targeted_search"}]
    assert searches
    assert all(s.top_score is not None for s in searches)
    assert any(s.retrieved_clause_ids for s in searches)


def test_restricted_use_drives_a_second_targeted_search(built_index, valid_fnol):
    """The section 4.1 mechanism: after the grant is found, the agent goes
    looking for what might displace it, using the policy's vocabulary."""
    result = _run(_delivery(valid_fnol))
    steps = [s.step for s in result.steps]
    assert "plan_modifiers" in steps
    assert "targeted_search" in steps
    actions = " ".join(s.tool_query or "" for s in result.steps).lower()
    assert "limitations as to use" in actions or "general exceptions" in actions


def test_restricted_use_routes_to_investigation(built_index, valid_fnol):
    """The deterministic floor holds regardless of what the model says."""
    result = _run(_delivery(valid_fnol))
    assert result.routing.queue in {RoutingQueue.investigation, RoutingQueue.human_escalation}


# -----------------------------------------------------------------------------
# Loop bounds (A1)
# -----------------------------------------------------------------------------


def test_tool_calls_stay_within_the_budget(built_index, valid_fnol, monkeypatch):
    from claimtriage.config import get_settings

    budget = get_settings().agent_max_tool_calls
    result = _run(_delivery(valid_fnol))
    assert result.tool_calls <= budget


def test_steps_stay_within_a_sane_bound(built_index, valid_fnol):
    from claimtriage.config import get_settings

    # The graph may add a terminal escalation step after the bound is detected,
    # so the assertion allows a small margin over the configured maximum.
    result = _run(_delivery(valid_fnol))
    assert len(result.steps) <= get_settings().agent_max_steps + 3


def test_retry_uses_a_different_strategy_each_time(built_index, valid_fnol):
    """A3: 'retry with a different strategy'. Retrying identically is how a
    system convinces itself."""
    result = _run(_delivery(valid_fnol))
    replans = [s for s in result.steps if s.step == "replan"]
    strategies = [s.detail.get("next_strategy") for s in replans]
    assert len(strategies) == len(set(strategies)), strategies
    assert all(s in SEARCH_STRATEGIES for s in strategies if s)


def test_wall_clock_bound_is_respected(built_index, valid_fnol, monkeypatch):
    monkeypatch.setenv("AGENT_WALL_CLOCK_SECONDS", "0.3")
    from claimtriage.config import reload_settings

    reload_settings()
    try:
        result = _run(valid_fnol)
        # Whatever happens, we get a result object rather than a hang or a raise.
        assert result.status in {
            RunStatus.completed,
            RunStatus.escalated,
            RunStatus.failed,
        }
    finally:
        monkeypatch.delenv("AGENT_WALL_CLOCK_SECONDS", raising=False)
        reload_settings()


# -----------------------------------------------------------------------------
# Resilience and degradation (A5)
# -----------------------------------------------------------------------------


class _Broken:
    """A provider that always fails, retryably."""

    name = "broken"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, json_mode, max_tokens, temperature):
        self.calls += 1
        raise ProviderError("injected failure", retryable=True, status=503)

    def healthy(self) -> bool:
        return False


class _Garbage:
    """A provider that returns something that is not JSON."""

    name = "garbage"

    def complete(self, messages, *, json_mode, max_tokens, temperature):
        return "I'm afraid I can't do that, Dave.", Usage(10, 10), "garbage-1", {}

    def healthy(self) -> bool:
        return True


def test_provider_outage_escalates_rather_than_guessing(built_index, valid_fnol):
    provider = _Broken()
    result = _run(valid_fnol, client=LLMClient(provider=provider))
    assert result.status in {RunStatus.escalated, RunStatus.failed}
    assert result.routing.queue == RoutingQueue.human_escalation
    assert result.escalation.required
    assert result.degraded or result.error


def test_circuit_breaker_opens_and_then_short_circuits(built_index, valid_fnol):
    """The breaker counts attempts, not logical calls.

    With `LLM_MAX_ATTEMPTS=3`, one `complete()` against a dead provider makes
    three real requests and therefore records three failures. So a
    threshold-of-2 breaker opens part-way through the first call and that call
    surfaces as `CircuitOpen` rather than `ProviderError` -- which is the
    outcome we want, because the agent treats `CircuitOpen` as
    "provider unavailable, escalate" rather than as a per-claim failure.
    """
    provider = _Broken()
    client = LLMClient(provider=provider)
    client.breaker.threshold = 2

    with pytest.raises((ProviderError, CircuitOpen)):
        client.complete([{"role": "user", "content": "x"}], step="probe")
    assert client.breaker.state == client.breaker.OPEN

    before = provider.calls
    with pytest.raises(CircuitOpen):
        client.complete([{"role": "user", "content": "x"}], step="probe")
    assert provider.calls == before, "an open circuit must not reach the provider"


def test_circuit_breaker_half_opens_after_the_reset_window(built_index):
    provider = _Broken()
    client = LLMClient(provider=provider)
    client.breaker.reset_seconds = 0.05
    client.breaker.force_open()
    assert client.breaker.state == client.breaker.OPEN
    time.sleep(0.1)
    assert client.breaker.state == client.breaker.HALF_OPEN


def test_circuit_breaker_closes_on_success(built_index):
    client = LLMClient()
    client.breaker.force_open()
    client.breaker.reset()
    assert client.breaker.state == client.breaker.CLOSED
    completion = client.complete(
        [{"role": "user", "content": "[[STEP:verification]] check this"}], step="verification"
    )
    assert completion.text
    assert client.breaker.state == client.breaker.CLOSED


def test_forced_circuit_open_is_demonstrable(built_index, valid_fnol):
    """Demonstration item 8: trip the breaker without taking the provider down."""
    client = LLMClient()
    client.breaker.force_open()
    result = _run(valid_fnol, client=client)
    assert result.escalation.required
    assert result.routing.queue == RoutingQueue.human_escalation
    client.breaker.reset()
    assert client.breaker.state == client.breaker.CLOSED


def test_unparseable_model_output_does_not_crash_the_run(built_index, valid_fnol):
    result = _run(valid_fnol, client=LLMClient(provider=_Garbage()))
    assert result.status in {RunStatus.escalated, RunStatus.failed}
    assert result.routing is not None


def test_retries_are_bounded(built_index):
    from claimtriage.config import get_settings

    provider = _Broken()
    client = LLMClient(provider=provider)
    with pytest.raises(ProviderError):
        client.complete([{"role": "user", "content": "x"}], step="probe")
    assert provider.calls == get_settings().llm_max_attempts


# -----------------------------------------------------------------------------
# Caching (D7)
# -----------------------------------------------------------------------------


def test_run_cache_key_ignores_the_claim_reference(valid_fnol):
    a = copy.deepcopy(valid_fnol)
    b = copy.deepcopy(valid_fnol)
    b["claim_reference"] = "TEST-0000000002"
    assert run_cache_key(a) == run_cache_key(b)


def test_run_cache_key_changes_with_the_claim(valid_fnol):
    a = copy.deepcopy(valid_fnol)
    b = copy.deepcopy(valid_fnol)
    b["incident"]["estimated_repair_cost_inr"] = 99999.0
    assert run_cache_key(a) != run_cache_key(b)


def test_run_cache_key_changes_with_the_prompt_set(valid_fnol, monkeypatch):
    from claimtriage.config import reload_settings
    from claimtriage.prompts import reset_registry

    first = run_cache_key(valid_fnol)
    monkeypatch.setenv("PROMPT_SET", "v1")
    reset_registry()
    reload_settings()
    try:
        assert run_cache_key(valid_fnol) != first
    finally:
        monkeypatch.delenv("PROMPT_SET", raising=False)
        reset_registry()
        reload_settings()


# -----------------------------------------------------------------------------
# Output guardrail at the very end (L4)
# -----------------------------------------------------------------------------


def test_unauthorised_determination_is_withheld(built_index, valid_fnol):
    """If the model drafts an approval, it never leaves the process."""
    from claimtriage.agents.runner import _apply_output_guardrail
    from claimtriage.contracts.models import (
        CoverageDetermination,
        CoveragePosition,
        RoutingDecision,
        TriageResult,
    )

    result = TriageResult(
        run_id="run-x",
        correlation_id="cid-x",
        claim_reference="TEST-0000000001",
        status=RunStatus.completed,
        submitted_at="2025-11-04T19:40:00Z",
        coverage=CoverageDetermination(
            position=CoveragePosition.likely_covered,
            summary="The claim is approved and we will pay the full amount.",
            reasoning="Settled under section I.",
        ),
        routing=RoutingDecision(queue=RoutingQueue.fast_track, reason="low value"),
    )
    filtered = _apply_output_guardrail(result)
    assert filtered.status == RunStatus.escalated
    assert filtered.routing.queue == RoutingQueue.human_escalation
    assert filtered.coverage.position == CoveragePosition.undetermined
    assert "approved" not in filtered.coverage.summary.lower()
    assert filtered.guardrail_events


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _delivery(fnol: dict) -> dict:
    out = copy.deepcopy(fnol)
    out["claim_reference"] = "TEST-DELIVERY-001"
    out["incident"]["vehicle_use_at_time_of_loss"] = "food_or_parcel_delivery"
    out["incident"]["description"] = (
        "I was making a food delivery run in my own car when the accident happened. "
        "I braked for a dog and hit the kerb. The bumper and bonnet are damaged."
    )
    out["incident"]["estimated_repair_cost_inr"] = 48000.0
    return out


def _out_of_corpus(fnol: dict) -> dict:
    out = copy.deepcopy(fnol)
    out["claim_reference"] = "TEST-OUTCORPUS-01"
    out["incident"]["description"] = (
        "Someone cloned my car's digital key through the companion mobile app and "
        "used it for two hours before returning the car undamaged."
    )
    return out
