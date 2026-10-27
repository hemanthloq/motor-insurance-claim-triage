"""The evaluation harness and its metrics (L2, L3).

Two things are asserted here. First, the metrics behave the way their
definitions claim -- in particular that escalating is not counted as a
hallucination, because a metric that punishes declining to answer would push
the system in exactly the wrong direction. Second, the harness itself refuses
to produce a report against the stub provider unless explicitly overridden,
since a number measured against the stub is a number about the stub.
"""

from __future__ import annotations

import json

import pytest

from claimtriage.eval.harness import (
    StubProviderRefused,
    compare_reports,
    deep_merge,
    load_dataset,
    run_evaluation,
)
from claimtriage.eval.metrics import (
    GROUNDEDNESS_THRESHOLD,
    aggregate,
    content_words,
    recall,
    score_record,
    sentences,
)


# -----------------------------------------------------------------------------
# Dataset
# -----------------------------------------------------------------------------


def test_dataset_has_at_least_twenty_scenarios():
    data = load_dataset("eval/dataset.yaml")
    assert len(data["scenarios"]) >= 20, "L3 requires at least 20 scenarios"


def test_dataset_scenarios_are_well_formed():
    data = load_dataset("eval/dataset.yaml")
    seen = set()
    for scenario in data["scenarios"]:
        assert scenario["id"] not in seen, f"duplicate scenario id {scenario['id']}"
        seen.add(scenario["id"])
        assert scenario.get("title")
        assert scenario.get("category")
        assert scenario.get("expect"), f"{scenario['id']} has no expected outcome"


def test_the_reference_case_is_present_and_names_its_supporting_passages():
    """Section 4.1 must be in the set, with the passages the answer rests on."""
    data = load_dataset("eval/dataset.yaml")
    reference = [s for s in data["scenarios"] if s.get("is_reference_case")]
    assert len(reference) == 1, "exactly one scenario should be the reference case"
    scenario = reference[0]
    assert scenario["patch"]["incident"]["vehicle_use_at_time_of_loss"] == "food_or_parcel_delivery"
    clauses = " ".join(p["clause"] for p in scenario["supporting_passage"]).lower()
    assert "limitations as to use" in clauses
    assert "general exception" in clauses


def test_dataset_covers_the_required_behaviours():
    data = load_dataset("eval/dataset.yaml")
    categories = {s["category"] for s in data["scenarios"]}
    for required in ("restricted_use", "out_of_corpus", "guardrail", "validation"):
        assert required in categories, f"no {required} scenario in the evaluation set"


def test_every_scenario_patch_merges_into_a_valid_or_deliberately_invalid_fnol():
    from claimtriage.validation import validate_submission

    data = load_dataset("eval/dataset.yaml")
    base = data["base"]
    for scenario in data["scenarios"]:
        merged = deep_merge(base, scenario.get("patch") or {})
        outcome = validate_submission(merged, correlation_id="cid-eval", client_id="pytest")
        expects_rejection = "rejected" in str(scenario["expect"].get("outcome", ""))
        if expects_rejection:
            # Either the contract or the guardrail must reject it. Guardrail
            # scenarios pass contract validation by design.
            if outcome.ok:
                from claimtriage.guardrails import check_fnol

                assert not check_fnol(merged).allowed, (
                    f"{scenario['id']} expects rejection but nothing rejects it"
                )
        else:
            assert outcome.ok, (
                f"{scenario['id']} should be valid but failed: "
                f"{[i.model_dump() for i in outcome.errors]}"
            )


def test_deep_merge_does_not_mutate_the_base():
    base = {"a": {"b": 1, "c": 2}}
    merged = deep_merge(base, {"a": {"b": 9}})
    assert merged == {"a": {"b": 9, "c": 2}}
    assert base == {"a": {"b": 1, "c": 2}}


# -----------------------------------------------------------------------------
# Metric primitives
# -----------------------------------------------------------------------------


def test_content_words_drops_stopwords_and_domain_filler():
    words = content_words("The company shall not be liable under this policy for the claim")
    assert "liable" in words
    assert "the" not in words and "policy" not in words and "claim" not in words


def test_recall_is_one_on_an_exact_match():
    statement = "organised racing pace making speed testing"
    passage = "Organised racing, Pace Making, Reliability trails or Speed testing"
    assert recall(statement, passage) == pytest.approx(1.0)


def test_recall_is_high_but_not_perfect_across_inflections():
    """A stated limitation of the lexical scorer.

    "used" and "use" are the same word to a reader and different tokens to
    this function, because there is no stemmer. The effect is that the lexical
    groundedness number is a slight *under*-estimate, which is the direction we
    would choose if we had to choose -- it will not flatter the system. It is
    also why the LLM-judge pass exists, and why docs/evaluation.md reports both
    numbers rather than one.
    """
    statement = "the vehicle was used for hire or reward"
    passage = "The Policy covers use of the vehicle for any purpose other than: Hire or Reward"
    score = recall(statement, passage)
    assert 0.6 <= score < 1.0


def test_recall_is_zero_for_unrelated_text():
    assert recall("organised racing and speed testing", "notice shall be given immediately") == 0.0


def test_sentences_splits_and_drops_fragments():
    text = "The grant applies here. But the restriction displaces it. Short."
    parts = sentences(text)
    assert len(parts) == 2
    assert all(len(p) > 20 for p in parts)


# -----------------------------------------------------------------------------
# score_record
# -----------------------------------------------------------------------------


def _passage(text: str, clause_id: str = "doc::sec::0001") -> dict:
    return {"clause_id": clause_id, "text": text}


GRANT = (
    "The Company will indemnify the insured against loss or damage to the vehicle "
    "insured caused by accidental external means."
)


def test_a_grounded_answer_scores_high():
    result = {
        "status": "completed",
        "coverage": {
            "position": "likely_covered",
            "summary": "The loss or damage to the vehicle insured was caused by accidental external means.",
            "reasoning": "The Company will indemnify the insured against loss or damage caused by accidental external means.",
        },
        "citations": [{"clause_id": "doc::sec::0001", "quote": GRANT, "resolves": True}],
        "verification": {"citations_checked": 1},
        "escalation": {"required": False},
    }
    score = score_record(
        result=result,
        retrieved_passages=[_passage(GRANT)],
        claim_text="accidental external means damage to the vehicle",
    )
    assert score.groundedness >= GROUNDEDNESS_THRESHOLD
    assert score.faithfulness == pytest.approx(1.0)
    assert score.hallucinated is False


def test_an_invented_statement_is_not_grounded():
    result = {
        "status": "completed",
        "coverage": {
            "position": "likely_covered",
            "summary": "Market practice in India is to settle these within fourteen working days regardless.",
            "reasoning": "Insurers customarily waive the compulsory deductible for first-time claimants.",
        },
        "citations": [{"clause_id": "doc::sec::0001", "quote": GRANT, "resolves": True}],
        "verification": {"citations_checked": 1},
        "escalation": {"required": False},
    }
    score = score_record(
        result=result, retrieved_passages=[_passage(GRANT)], claim_text="deductible"
    )
    assert score.groundedness < GROUNDEDNESS_THRESHOLD
    assert score.detail["ungrounded_statements"]


def test_an_unresolved_citation_is_a_hallucination():
    result = {
        "status": "completed",
        "coverage": {"position": "likely_not_covered", "summary": "Excluded.", "reasoning": "Because."},
        "citations": [{"clause_id": "doc::sec::0001", "quote": "invented words", "resolves": False}],
        "verification": {"citations_checked": 1},
        "escalation": {"required": False},
    }
    score = score_record(result=result, retrieved_passages=[_passage(GRANT)], claim_text="x")
    assert score.hallucinated is True
    assert score.faithfulness == 0.0


def test_a_confident_answer_with_no_citation_is_a_hallucination():
    result = {
        "status": "completed",
        "coverage": {"position": "likely_covered", "summary": "Covered.", "reasoning": "It is."},
        "citations": [],
        "verification": {"citations_checked": 0},
        "escalation": {"required": False},
    }
    score = score_record(result=result, retrieved_passages=[_passage(GRANT)], claim_text="x")
    assert score.hallucinated is True


def test_escalating_is_not_a_hallucination():
    """The metric must not punish declining to answer -- F7 requires it."""
    result = {
        "status": "escalated",
        "coverage": {
            "position": "undetermined",
            "summary": "The wording does not address this loss.",
            "reasoning": "No retrieved passage bears on the question.",
        },
        "citations": [],
        "verification": {"citations_checked": 0},
        "escalation": {"required": True, "reason": "outside_corpus"},
    }
    score = score_record(result=result, retrieved_passages=[], claim_text="x")
    assert score.hallucinated is False
    assert score.faithfulness == pytest.approx(1.0)


def test_undetermined_is_not_a_hallucination():
    result = {
        "status": "completed",
        "coverage": {"position": "undetermined", "summary": "Unclear.", "reasoning": "Conflicting."},
        "citations": [],
        "verification": {"citations_checked": 0},
        "escalation": {"required": False},
    }
    assert score_record(result=result, retrieved_passages=[], claim_text="x").hallucinated is False


def test_context_relevance_penalises_unrelated_passages():
    result = {
        "status": "completed",
        "coverage": {"position": "likely_covered", "summary": GRANT, "reasoning": GRANT},
        "citations": [{"clause_id": "doc::sec::0001", "quote": GRANT, "resolves": True}],
        "verification": {"citations_checked": 1},
        "escalation": {"required": False},
    }
    relevant = score_record(
        result=result,
        retrieved_passages=[_passage(GRANT)],
        claim_text="accidental external means damage to the vehicle insured",
    ).context_relevance
    irrelevant = score_record(
        result=result,
        retrieved_passages=[
            _passage("The grievance redressal officer may be contacted at the address below.")
        ],
        claim_text="accidental external means damage to the vehicle insured",
    ).context_relevance
    assert relevant > irrelevant


def test_aggregate_handles_an_empty_set():
    assert aggregate([]) == {
        "groundedness": 0.0,
        "context_relevance": 0.0,
        "faithfulness": 0.0,
        "hallucination_rate": 0.0,
    }


# -----------------------------------------------------------------------------
# Harness behaviour
# -----------------------------------------------------------------------------


def test_harness_refuses_to_report_against_the_stub(built_index):
    """A metric measured against the stub is a metric about the stub."""
    with pytest.raises(StubProviderRefused):
        run_evaluation(dataset_path="eval/dataset.yaml", only=["S03_minor_own_damage_fast_track"])


def test_harness_runs_with_an_explicit_override_and_stamps_the_report(built_index, tmp_path):
    out = tmp_path / "report.json"
    report = run_evaluation(
        dataset_path="eval/dataset.yaml",
        out_path=out,
        allow_stub=True,
        only=["S03_minor_own_damage_fast_track", "S22_loss_outside_policy_period"],
    )
    assert report["scenarios"] == 2
    assert report["valid_for_reporting"] is False
    assert report["stub_override_used"] is True
    assert set(report["metrics"]) == {
        "groundedness",
        "context_relevance",
        "faithfulness",
        "hallucination_rate",
    }
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["scenarios"] == 2
    assert written["configuration"]["prompt_set"]


def test_harness_grades_a_boundary_rejection_without_starting_a_run(built_index):
    report = run_evaluation(
        dataset_path="eval/dataset.yaml",
        allow_stub=True,
        only=["S22_loss_outside_policy_period", "S20_prompt_injection_in_description"],
    )
    for record in report["records"]:
        assert record["actual_outcome"] == "rejected"
        assert record["agent_run_started"] is False
        assert record["passed"], record["failures"]


def test_harness_reports_the_reference_case_separately(built_index):
    report = run_evaluation(
        dataset_path="eval/dataset.yaml",
        allow_stub=True,
        only=["S01_food_delivery_reference_case"],
    )
    assert report["reference_case"] is not None
    assert report["reference_case"]["id"] == "S01_food_delivery_reference_case"


def test_unknown_scenario_id_is_an_error(built_index):
    with pytest.raises(ValueError):
        run_evaluation(dataset_path="eval/dataset.yaml", allow_stub=True, only=["nope"])


# -----------------------------------------------------------------------------
# Report comparison (L2, L3 before/after)
# -----------------------------------------------------------------------------


def test_compare_reports_reads_the_direction_of_each_metric_correctly():
    """Higher is better for three of them and worse for the fourth. Getting
    this backwards would make a regression look like an improvement."""
    before = {
        "label": "before",
        "metrics": {
            "groundedness": 0.60,
            "context_relevance": 0.40,
            "faithfulness": 0.70,
            "hallucination_rate": 0.20,
        },
        "summary": {"pass_rate": 0.70, "total_cost_usd": 0.01},
        "records": [{"id": "A", "passed": True}, {"id": "B", "passed": False}],
    }
    after = {
        "label": "after",
        "metrics": {
            "groundedness": 0.80,
            "context_relevance": 0.35,
            "faithfulness": 0.70,
            "hallucination_rate": 0.05,
        },
        "summary": {"pass_rate": 0.85, "total_cost_usd": 0.02},
        "records": [{"id": "A", "passed": False, "failures": ["x"]}, {"id": "B", "passed": True}],
    }
    diff = compare_reports(before, after)
    assert diff["metrics"]["groundedness"]["improved"] is True
    assert diff["metrics"]["context_relevance"]["improved"] is False
    assert diff["metrics"]["faithfulness"]["improved"] is False  # unchanged
    assert diff["metrics"]["hallucination_rate"]["improved"] is True  # lower is better
    assert diff["metrics"]["groundedness"]["relative_pct"] == pytest.approx(33.33, abs=0.01)
    assert diff["pass_rate"]["delta"] == pytest.approx(0.15)
    assert [r["id"] for r in diff["regressions"]] == ["A"]
    assert [f["id"] for f in diff["fixed"]] == ["B"]
