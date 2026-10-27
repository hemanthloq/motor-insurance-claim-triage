"""The offline evaluation harness (L3, L2).

Runs the fixed evaluation set in-process against the real graph -- the same
nodes, prompts, tools and verifier the service runs -- and produces a report
carrying the four required metrics, the per-scenario outcome, and enough detail
to see *why* a scenario failed rather than only that it did.

It refuses to write a report when the stub provider is active. A stub cannot
read a policy clause, so numbers produced against it would be a measurement of
the stub. `--allow-stub` overrides this for smoke-testing the harness itself,
and the override is stamped on the report.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..config import get_settings
from ..contracts.models import RunStatus
from ..guardrails import check_fnol
from ..logging_setup import get_logger
from ..validation import validate_submission
from .metrics import RecordScore, aggregate, judge_record, score_record

log = get_logger("eval")


class StubProviderRefused(RuntimeError):
    pass


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_dataset(path: str | Path = "eval/dataset.yaml") -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not data or "scenarios" not in data:
        raise ValueError(f"{path} does not look like an evaluation set")
    return data


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in (value if isinstance(value, list) else [value])]


def run_scenario(scenario: dict[str, Any], base: dict[str, Any], *, judge: bool) -> dict[str, Any]:
    from ..agents.runner import execute_run
    from ..retrieval.index import get_index

    fnol = deep_merge(base, scenario.get("patch") or {})
    expect = scenario.get("expect") or {}
    scenario_id = scenario["id"]
    correlation_id = f"eval-{scenario_id}"
    started = time.perf_counter()

    record: dict[str, Any] = {
        "id": scenario_id,
        "title": scenario.get("title"),
        "category": scenario.get("category"),
        "is_reference_case": bool(scenario.get("is_reference_case")),
        "expected": expect,
    }

    # ---- the boundary runs first, exactly as it does in the service -------
    outcome = validate_submission(fnol, correlation_id=correlation_id, client_id="eval")
    guard = check_fnol(fnol) if outcome.ok else None

    if not outcome.ok or (guard is not None and not guard.allowed):
        stage = outcome.quarantine.stage if not outcome.ok else "guardrail"
        rules = (
            [i.rule for i in outcome.errors]
            if not outcome.ok
            else [f"guardrail:{f.pattern}" for f in (guard.findings if guard else [])]
        )
        record.update(
            {
                "actual_outcome": "rejected",
                "rejection_stage": stage,
                "rejection_rules": rules,
                "agent_run_started": False,
                "duration_seconds": round(time.perf_counter() - started, 4),
            }
        )
        record["passed"], record["failures"] = _grade_rejection(expect, record)
        return record

    warnings = [w.model_dump(mode="json") for w in outcome.warnings]
    result = execute_run(
        run_id=f"eval-{scenario_id}-{uuid.uuid4().hex[:6]}",
        correlation_id=correlation_id,
        claim=outcome.fnol.model_dump(mode="json"),  # type: ignore[union-attr]
        warnings=warnings,
        use_cache=False,
    )
    payload = result.model_dump(mode="json")

    # Reconstruct what the model actually saw, for the metrics.
    index = get_index()
    clause_ids: list[str] = []
    for step in payload.get("steps") or []:
        for clause_id in step.get("retrieved_clause_ids") or []:
            if clause_id not in clause_ids:
                clause_ids.append(clause_id)
    passages = []
    for clause_id in clause_ids:
        hit = index.get_clause(clause_id)
        if hit:
            passages.append({"clause_id": hit.clause_id, "text": hit.text, "section_path": hit.section_path})

    hints = [
        str(p.get("clause", "")) for p in (scenario.get("supporting_passage") or [])
    ]
    score = score_record(
        result=payload,
        retrieved_passages=passages,
        claim_text=(fnol.get("incident") or {}).get("description", ""),
        expected_clause_hints=hints,
    )

    record.update(
        {
            "actual_outcome": payload["status"],
            "agent_run_started": True,
            "position": (payload.get("coverage") or {}).get("position"),
            "queue": (payload.get("routing") or {}).get("queue"),
            "verification": (payload.get("verification") or {}).get("verdict"),
            "support_score": (payload.get("verification") or {}).get("support_score"),
            "escalation_reason": (payload.get("escalation") or {}).get("reason"),
            "citations": [
                {
                    "clause_id": c["clause_id"],
                    "section_path": c["section_path"],
                    "page": c.get("page"),
                    "role": c.get("role"),
                    "quote": c["quote"][:200],
                }
                for c in payload.get("citations") or []
            ],
            "warnings": [w.get("rule") for w in warnings],
            "steps": len(payload.get("steps") or []),
            "tool_calls": payload.get("tool_calls"),
            "tokens": (payload.get("cost") or {}).get("total_tokens"),
            "cost_usd": (payload.get("cost") or {}).get("cost_usd"),
            "degraded": payload.get("degraded"),
            "prompt_versions": payload.get("prompt_versions"),
            "duration_seconds": round(time.perf_counter() - started, 4),
            "scores": score.to_dict(),
            "passages_retrieved": len(passages),
        }
    )

    if judge:
        verdict = judge_record(
            result=payload,
            retrieved_passages=passages,
            claim_text=(fnol.get("incident") or {}).get("description", ""),
        )
        if verdict:
            record["judge"] = verdict

    record["passed"], record["failures"] = _grade_run(expect, record, payload)
    record["_score_object"] = score
    return record


def _grade_rejection(expect: dict[str, Any], record: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    wanted = _as_list(expect.get("outcome")) or ["completed"]
    if "rejected" not in wanted:
        failures.append(f"expected outcome {wanted}, the submission was rejected at the boundary")
    stage = expect.get("rejection_stage")
    if stage and record.get("rejection_stage") != stage:
        failures.append(f"expected rejection at {stage}, got {record.get('rejection_stage')}")
    for rule in _as_list(expect.get("rejection_rules")):
        if not any(rule in r for r in record.get("rejection_rules") or []):
            failures.append(f"expected validation rule {rule} to fire")
    if expect.get("agent_run_started") is False and record.get("agent_run_started"):
        failures.append("an agent run started for a submission that should have been rejected")
    return not failures, failures


def _grade_run(
    expect: dict[str, Any], record: dict[str, Any], payload: dict[str, Any]
) -> tuple[bool, list[str]]:
    failures: list[str] = []

    wanted_outcome = _as_list(expect.get("outcome")) or ["completed"]
    if record["actual_outcome"] not in wanted_outcome:
        failures.append(
            f"outcome {record['actual_outcome']} not in expected {wanted_outcome}"
        )

    wanted_position = _as_list(expect.get("position"))
    if wanted_position and record.get("position") not in wanted_position:
        failures.append(f"position {record.get('position')} not in expected {wanted_position}")

    wanted_queue = _as_list(expect.get("queue"))
    if wanted_queue and record.get("queue") not in wanted_queue + ["human_escalation"]:
        failures.append(f"queue {record.get('queue')} not in expected {wanted_queue}")

    minimum = int(expect.get("min_citations") or 0)
    if record["actual_outcome"] == "completed" and len(record.get("citations") or []) < minimum:
        failures.append(
            f"expected at least {minimum} resolving citation(s), got {len(record.get('citations') or [])}"
        )

    needles = [n.lower() for n in _as_list(expect.get("must_cite_section_any"))]
    if needles and record["actual_outcome"] == "completed":
        haystack = " ".join(
            (c["section_path"] + " " + c["quote"]).lower() for c in record.get("citations") or []
        )
        if not any(n in haystack for n in needles):
            failures.append(f"no citation touched any of {needles}")

    mentions = [m.lower() for m in _as_list(expect.get("must_mention_any"))]
    if mentions and record["actual_outcome"] == "completed":
        coverage = payload.get("coverage") or {}
        prose = " ".join(
            [
                str(coverage.get("summary", "")),
                str(coverage.get("reasoning", "")),
                str((payload.get("routing") or {}).get("reason", "")),
                " ".join(str(q) for q in coverage.get("open_questions") or []),
            ]
        ).lower()
        if not any(m in prose for m in mentions):
            failures.append(f"the determination mentioned none of {mentions}")

    for warning in _as_list(expect.get("warnings_include")):
        if warning not in (record.get("warnings") or []):
            failures.append(f"expected validation warning {warning}")

    allowed_reasons = _as_list(expect.get("acceptable_escalation_reasons"))
    if allowed_reasons and record["actual_outcome"] == "escalated":
        if record.get("escalation_reason") not in allowed_reasons:
            failures.append(
                f"escalation reason {record.get('escalation_reason')} not in {allowed_reasons}"
            )

    # Unconditional invariants -- true of every run, in every scenario.
    for citation in payload.get("citations") or []:
        if not citation.get("resolves"):
            failures.append(f"citation {citation.get('clause_id')} was returned without resolving")
    if payload.get("status") == "completed" and (payload.get("verification") or {}).get(
        "verdict"
    ) != "verified":
        failures.append("a completed run was returned without a verified determination")

    return not failures, failures


def run_evaluation(
    *,
    dataset_path: str | Path = "eval/dataset.yaml",
    out_path: str | Path | None = None,
    judge: bool = False,
    allow_stub: bool = False,
    only: list[str] | None = None,
    prompt_set: str | None = None,
    label: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    if settings.llm_provider == "stub" and not allow_stub:
        raise StubProviderRefused(
            "LLM_PROVIDER=stub. The stub cannot read a policy clause, so any "
            "metric produced against it measures the stub, not the system. "
            "Configure a real provider, or pass --allow-stub to smoke-test the "
            "harness (the report will be stamped as not valid for reporting)."
        )

    if prompt_set:
        import os

        os.environ["PROMPT_SET"] = prompt_set
        from ..config import reload_settings
        from ..prompts import reset_registry

        reset_registry()
        settings = reload_settings()

    data = load_dataset(dataset_path)
    base = data.get("base") or {}
    scenarios = data["scenarios"]
    if only:
        scenarios = [s for s in scenarios if s["id"] in set(only)]
        if not scenarios:
            raise ValueError(f"no scenarios matched {only}")

    started = datetime.now(timezone.utc)
    wall = time.perf_counter()
    records: list[dict[str, Any]] = []
    for i, scenario in enumerate(scenarios, start=1):
        log.info("eval_scenario_start", n=f"{i}/{len(scenarios)}", id=scenario["id"])
        try:
            record = run_scenario(scenario, base, judge=judge)
        except Exception as exc:  # a harness failure is not a system failure
            log.exception("eval_scenario_error", id=scenario["id"], error=str(exc))
            record = {
                "id": scenario["id"],
                "title": scenario.get("title"),
                "category": scenario.get("category"),
                "actual_outcome": "harness_error",
                "passed": False,
                "failures": [f"harness error: {exc}"],
            }
        records.append(record)
        log.info(
            "eval_scenario_done",
            id=record["id"],
            passed=record.get("passed"),
            outcome=record.get("actual_outcome"),
            failures=record.get("failures"),
        )

    scored = [r.pop("_score_object") for r in records if "_score_object" in r]
    metrics = aggregate(scored)

    by_category: dict[str, dict[str, int]] = {}
    for record in records:
        bucket = by_category.setdefault(record.get("category") or "uncategorised", {"passed": 0, "failed": 0})
        bucket["passed" if record.get("passed") else "failed"] += 1

    agent_runs = [r for r in records if r.get("agent_run_started")]
    report = {
        "label": label or f"{settings.prompt_set} @ {started.date()}",
        "started_at": started.isoformat(),
        "duration_seconds": round(time.perf_counter() - wall, 2),
        "dataset": str(dataset_path),
        "dataset_version": data.get("version"),
        "scenarios": len(records),
        "configuration": {
            "prompt_set": settings.prompt_set,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model or None,
            "embedding_backend": settings.embedding_backend,
            "vector_backend": settings.vector_backend,
            "retrieval_top_k": settings.retrieval_top_k,
            "verifier_min_support": settings.verifier_min_support,
            "agent_max_tool_calls": settings.agent_max_tool_calls,
            "judge_enabled": judge,
        },
        "valid_for_reporting": settings.llm_provider != "stub",
        "stub_override_used": settings.llm_provider == "stub" and allow_stub,
        "summary": {
            "passed": sum(1 for r in records if r.get("passed")),
            "failed": sum(1 for r in records if not r.get("passed")),
            "pass_rate": round(sum(1 for r in records if r.get("passed")) / len(records), 4)
            if records
            else 0.0,
            "by_category": by_category,
            "escalated": sum(1 for r in records if r.get("actual_outcome") == "escalated"),
            "rejected_at_boundary": sum(1 for r in records if r.get("actual_outcome") == "rejected"),
            "agent_runs": len(agent_runs),
            "mean_steps": round(
                sum(r.get("steps", 0) for r in agent_runs) / len(agent_runs), 2
            )
            if agent_runs
            else 0.0,
            "mean_tool_calls": round(
                sum(r.get("tool_calls", 0) or 0 for r in agent_runs) / len(agent_runs), 2
            )
            if agent_runs
            else 0.0,
            "mean_duration_seconds": round(
                sum(r.get("duration_seconds", 0) for r in agent_runs) / len(agent_runs), 3
            )
            if agent_runs
            else 0.0,
            "total_tokens": sum(r.get("tokens") or 0 for r in agent_runs),
            "total_cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in agent_runs), 6),
        },
        "metrics": metrics,
        "reference_case": next(
            (
                {
                    "id": r["id"],
                    "passed": r.get("passed"),
                    "position": r.get("position"),
                    "queue": r.get("queue"),
                    "citations": r.get("citations"),
                    "failures": r.get("failures"),
                }
                for r in records
                if r.get("is_reference_case")
            ),
            None,
        ),
        "records": records,
    }

    if judge:
        judged = [r["judge"] for r in records if isinstance(r.get("judge"), dict)]
        if judged:
            report["judge_metrics"] = {
                key: round(
                    sum(float(j.get(key, 0.0)) for j in judged) / len(judged), 4
                )
                for key in ("groundedness", "context_relevance", "faithfulness")
            } | {
                "hallucination_rate": round(
                    sum(1 for j in judged if j.get("hallucinated")) / len(judged), 4
                ),
                "records_judged": len(judged),
            }

    if out_path:
        destination = Path(out_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        log.info("eval_report_written", path=str(destination))

    return report


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Side-by-side of two evaluation reports (L2, L3)."""
    keys = ("groundedness", "context_relevance", "faithfulness", "hallucination_rate")
    deltas = {}
    for key in keys:
        a = float(before.get("metrics", {}).get(key, 0.0))
        b = float(after.get("metrics", {}).get(key, 0.0))
        better = (b < a) if key == "hallucination_rate" else (b > a)
        deltas[key] = {
            "before": round(a, 4),
            "after": round(b, 4),
            "delta": round(b - a, 4),
            "relative_pct": round(((b - a) / a * 100), 2) if a else None,
            "improved": bool(better and abs(b - a) > 1e-9),
        }

    before_pass = before.get("summary", {}).get("pass_rate", 0.0)
    after_pass = after.get("summary", {}).get("pass_rate", 0.0)

    before_by_id = {r["id"]: r for r in before.get("records", [])}
    after_by_id = {r["id"]: r for r in after.get("records", [])}
    regressions, fixes = [], []
    for scenario_id, after_record in after_by_id.items():
        before_record = before_by_id.get(scenario_id)
        if not before_record:
            continue
        if before_record.get("passed") and not after_record.get("passed"):
            regressions.append({"id": scenario_id, "now_failing": after_record.get("failures")})
        if not before_record.get("passed") and after_record.get("passed"):
            fixes.append({"id": scenario_id, "was_failing": before_record.get("failures")})

    return {
        "before_label": before.get("label"),
        "after_label": after.get("label"),
        "metrics": deltas,
        "pass_rate": {
            "before": before_pass,
            "after": after_pass,
            "delta": round(after_pass - before_pass, 4),
        },
        "cost": {
            "before_usd": before.get("summary", {}).get("total_cost_usd"),
            "after_usd": after.get("summary", {}).get("total_cost_usd"),
        },
        "fixed": fixes,
        "regressions": regressions,
    }
