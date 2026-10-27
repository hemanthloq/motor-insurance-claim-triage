"""Evaluation metrics (L3).

Four metrics are required, and each one is stated here with exactly how it is
computed, because "groundedness 0.87" means nothing without that.

Why this and not TruLens
------------------------
TruLens is the obvious choice and we started there. Two things pushed us off
it. Its feedback functions are themselves model calls -- four of them per
record -- which on a free tier with a low rate limit means an evaluation run
over 24 scenarios costs about a hundred extra calls and takes longer than the
runs it is scoring. And the feedback functions are opaque at exactly the point
where a claims system needs them not to be: when a coverage position is judged
ungrounded we have to be able to say which sentence and which passage.

So this module implements the same four feedback functions, in the same terms,
with two scorers per metric:

  lexical  -- deterministic, free, runs anywhere, used for every record
  judge    -- an LLM-as-judge pass, run only when `--judge llm` is passed and a
              real provider is configured

The report carries both when the judge is enabled, so the deterministic numbers
are always comparable across runs and the judged numbers are there when a
sharper read is wanted. `docs/evaluation.md` reports both.

Definitions
-----------

groundedness
    Of the atomic statements in the determination (summary + reasoning, split
    on sentence boundaries), the share for which some retrieved passage
    supports the statement. Support, lexically, is content-word recall of the
    statement against the best-matching passage, at or above 0.45. The
    threshold was set by labelling 40 statements by hand and choosing the point
    that separated them best; it is a knob, and it is written down.

context_relevance
    Of the passages put in front of the model, the share that bear on the
    question. Lexically: content-word overlap between the passage and the
    claim text plus the expected-clause hints, at or above 0.12. Low, because a
    policy clause and a claim description share very little vocabulary by
    nature -- that is the whole difficulty of this corpus -- so this threshold
    detects "entirely unrelated", not "closely on point".

faithfulness
    Of the citations attached to the answer, the share whose quote is found
    character-for-character in the clause it is attributed to *and* whose
    clause was actually retrieved during the run. This is the strictest of the
    four and the one that cannot be argued with: a quote either appears in the
    source or it does not.

hallucination_rate
    The share of records where the system asserted something it could not
    support: a confident position (not `undetermined`, not escalated) that had
    either no resolving citation at all, or at least one citation that failed
    to resolve. Escalating is explicitly *not* a hallucination -- declining to
    answer is the correct behaviour under F7 and must not be punished by the
    metric that is supposed to reward honesty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..logging_setup import get_logger

log = get_logger("eval.metrics")

GROUNDEDNESS_THRESHOLD = 0.45
CONTEXT_RELEVANCE_THRESHOLD = 0.12

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WORD = re.compile(r"[a-z][a-z'-]+")

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by",
    "is", "are", "was", "were", "be", "been", "being", "this", "that", "these",
    "those", "it", "its", "as", "at", "from", "has", "have", "had", "not", "no",
    "any", "all", "such", "which", "shall", "will", "would", "may", "can",
    "under", "policy", "claim", "insured", "company", "vehicle",
}


def content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in STOPWORDS and len(w) > 2}


def recall(statement: str, passage: str) -> float:
    """Share of the statement's content words that appear in the passage."""
    needle = content_words(statement)
    if not needle:
        return 0.0
    return len(needle & content_words(passage)) / len(needle)


def sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE.split(text or "") if len(s.strip()) > 20]
    return parts or ([text.strip()] if (text or "").strip() else [])


@dataclass
class RecordScore:
    groundedness: float = 0.0
    context_relevance: float = 0.0
    faithfulness: float = 0.0
    hallucinated: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "groundedness": round(self.groundedness, 4),
            "context_relevance": round(self.context_relevance, 4),
            "faithfulness": round(self.faithfulness, 4),
            "hallucinated": self.hallucinated,
            **({"detail": self.detail} if self.detail else {}),
        }


def score_record(
    *,
    result: dict[str, Any],
    retrieved_passages: list[dict[str, Any]],
    claim_text: str,
    expected_clause_hints: Iterable[str] = (),
) -> RecordScore:
    coverage = result.get("coverage") or {}
    citations = result.get("citations") or []
    verification = result.get("verification") or {}
    status = result.get("status")

    # ---- groundedness ------------------------------------------------------
    passage_texts = [p.get("text", "") for p in retrieved_passages] + [
        c.get("quote", "") for c in citations
    ]
    statements = sentences(coverage.get("summary", "")) + sentences(coverage.get("reasoning", ""))
    grounded_flags = []
    ungrounded: list[str] = []
    for statement in statements:
        best = max((recall(statement, p) for p in passage_texts), default=0.0)
        ok = best >= GROUNDEDNESS_THRESHOLD
        grounded_flags.append(ok)
        if not ok:
            ungrounded.append(statement[:160])
    groundedness = sum(grounded_flags) / len(grounded_flags) if grounded_flags else 0.0

    # ---- context relevance -------------------------------------------------
    probe = claim_text + " " + " ".join(expected_clause_hints)
    relevant = [
        1.0 if recall(p.get("text", ""), probe) >= CONTEXT_RELEVANCE_THRESHOLD else 0.0
        for p in retrieved_passages
    ]
    context_relevance = sum(relevant) / len(relevant) if relevant else 0.0

    # ---- faithfulness ------------------------------------------------------
    resolved = sum(1 for c in citations if c.get("resolves"))
    checked = int(verification.get("citations_checked") or len(citations))
    faithfulness = resolved / checked if checked else (1.0 if status == "escalated" else 0.0)

    # ---- hallucination -----------------------------------------------------
    confident = (
        status == "completed"
        and coverage.get("position") not in (None, "undetermined")
        and not (result.get("escalation") or {}).get("required")
    )
    unresolved = checked - resolved
    hallucinated = bool(confident and (not resolved or unresolved > 0))

    return RecordScore(
        groundedness=groundedness,
        context_relevance=context_relevance,
        faithfulness=faithfulness,
        hallucinated=hallucinated,
        detail={
            "statements_scored": len(statements),
            "ungrounded_statements": ungrounded[:5],
            "passages_scored": len(retrieved_passages),
            "citations_resolved": f"{resolved}/{checked}",
        },
    )


def aggregate(scores: list[RecordScore]) -> dict[str, float]:
    if not scores:
        return {
            "groundedness": 0.0,
            "context_relevance": 0.0,
            "faithfulness": 0.0,
            "hallucination_rate": 0.0,
        }
    n = len(scores)
    return {
        "groundedness": round(sum(s.groundedness for s in scores) / n, 4),
        "context_relevance": round(sum(s.context_relevance for s in scores) / n, 4),
        "faithfulness": round(sum(s.faithfulness for s in scores) / n, 4),
        "hallucination_rate": round(sum(1 for s in scores if s.hallucinated) / n, 4),
    }


def score_live_run(result: dict[str, Any]) -> dict[str, float]:
    """Score one production run against the same four metrics (L6).

    The retrieved passages are reconstructed from the step records, which is
    the only evidence a finished run leaves behind -- another reason the trace
    records the clause ids each tool call returned.
    """
    from ..retrieval.index import get_index

    index = get_index()
    clause_ids: list[str] = []
    for step in result.get("steps") or []:
        for clause_id in step.get("retrieved_clause_ids") or []:
            if clause_id not in clause_ids:
                clause_ids.append(clause_id)

    passages = []
    for clause_id in clause_ids[:20]:
        hit = index.get_clause(clause_id)
        if hit:
            passages.append({"clause_id": hit.clause_id, "text": hit.text})

    claim_text = " ".join(
        str((c.get("quote") or "")) for c in (result.get("citations") or [])
    ) or (result.get("coverage") or {}).get("summary", "")

    score = score_record(
        result=result, retrieved_passages=passages, claim_text=claim_text
    )
    return {
        "groundedness": round(score.groundedness, 4),
        "context_relevance": round(score.context_relevance, 4),
        "faithfulness": round(score.faithfulness, 4),
        "hallucination_rate": 1.0 if score.hallucinated else 0.0,
    }


# -----------------------------------------------------------------------------
# Optional LLM-as-judge scorer
# -----------------------------------------------------------------------------

JUDGE_SYSTEM = """[[STEP:judge]]
You are grading a motor insurance coverage determination against the policy
passages that were available when it was written. You are strict. A statement is
supported only if a passage says it or necessarily implies it; being plausible,
or being true of insurance generally, is not support.

Return a single JSON object:
{
  "groundedness": 0.0-1.0,
  "context_relevance": 0.0-1.0,
  "faithfulness": 0.0-1.0,
  "hallucinated": true|false,
  "notes": "<one sentence>"
}
"""


def judge_record(
    *, result: dict[str, Any], retrieved_passages: list[dict[str, Any]], claim_text: str
) -> dict[str, Any] | None:
    """LLM-as-judge pass. Returns None when no real provider is configured."""
    from ..config import get_settings
    from ..llm import get_client, parse_json_object

    if get_settings().llm_provider == "stub":
        return None

    coverage = result.get("coverage") or {}
    context = "\n\n".join(
        f"[{p.get('clause_id')}]\n{p.get('text', '')[:1200]}" for p in retrieved_passages[:10]
    )
    citations = "\n".join(
        f"- {c.get('clause_id')}: {c.get('quote', '')[:200]}"
        for c in (result.get("citations") or [])
    )
    user = (
        f"CLAIM\n{claim_text}\n\nDETERMINATION\nposition: {coverage.get('position')}\n"
        f"summary: {coverage.get('summary')}\nreasoning: {coverage.get('reasoning')}\n\n"
        f"CITATIONS\n{citations or '(none)'}\n\nPASSAGES AVAILABLE\n{context or '(none)'}"
    )
    try:
        completion = get_client().complete(
            [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
            step="judge",
            json_mode=True,
        )
        return parse_json_object(completion.text)
    except Exception as exc:
        log.warning("judge_failed", error=str(exc)[:200])
        return None
