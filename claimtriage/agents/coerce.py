"""Normalise what a model returns into the shape the graph expects.

The prompts ask for a specific JSON object, and a well-behaved model returns
one. Real models drift, and the drift has a recognisable repertoire. Every one
of these was either seen or is a direct consequence of the code that read the
field before this module existed:

  * a string where a list belongs  -- `"queries": "limitations as to use"`.
    The graph then sliced it with `[:2]` and searched the corpus for the
    letters "l" and "i".
  * a list where a string belongs, or a dict where a list belongs
  * numbers as strings ("0.8"), as words ("high"), as percentages ("80%")
  * an integer with units ("8 hours"), which made `int()` raise mid-graph and
    failed the whole run
  * values outside the contract's range (0 hours, 900 hours), which made the
    pydantic result model reject the run at the very last step
  * the expected object wrapped one level down: `{"determination": {...}}`

None of these is worth a retry and none should fail a run. Each normaliser
returns a dict with exactly the keys its step reads, every one of the type the
step expects, and drops everything else.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_WORDS_TO_SCORE = {
    "very high": 0.9, "high": 0.8, "medium": 0.5, "moderate": 0.5,
    "low": 0.3, "very low": 0.1, "none": 0.0,
}


def unwrap(obj: Any, expected: Iterable[str]) -> dict:
    """Return the object that actually carries the expected keys.

    Handles `{"result": {...}}`, `{"determination": {...}}` and similar
    single-level wrappers, which models produce when they decide the answer
    deserves a title.
    """
    if not isinstance(obj, dict):
        return {}
    keys = set(expected)
    if keys & set(obj):
        return obj
    for value in obj.values():
        if isinstance(value, dict) and keys & set(value):
            return value
    return obj


def as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, dict):
        return list(value.values())
    return [value]


def as_str_list(value: Any, *, limit: int = 20) -> list[str]:
    out = []
    for item in as_list(value):
        if isinstance(item, (dict, list)):
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out[:limit]


def as_float(value: Any, default: float = 0.0, *, lo: float = 0.0, hi: float = 1.0) -> float:
    """Parse 0.8, "0.8", "80%", "high", "8/10" into a float within [lo, hi]."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        if text in _WORDS_TO_SCORE:
            number = _WORDS_TO_SCORE[text]
        else:
            match = _NUMBER.search(text)
            if not match:
                return default
            number = float(match.group())
            if "%" in text:
                number /= 100.0
            elif "/10" in text.replace(" ", ""):
                number /= 10.0
    else:
        return default
    if hi == 1.0 and 1.0 < number <= 100.0:
        number /= 100.0  # a confidence of "85" almost certainly means 0.85
    return max(lo, min(hi, number))


def as_int(value: Any, default: int, *, lo: int, hi: int) -> int:
    """Parse 8, "8", "8 hours", "8.0" into an int within [lo, hi]."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = int(round(float(value)))
    elif isinstance(value, str):
        match = _NUMBER.search(value)
        if not match:
            return default
        number = int(round(float(match.group())))
    else:
        return default
    return max(lo, min(hi, number))


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "y", "1"}:
            return True
        if text in {"false", "no", "n", "0"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def as_text(value: Any, *, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    return str(value).strip()[:limit]


# -----------------------------------------------------------------------------
# Per-step normalisers
# -----------------------------------------------------------------------------


def classification(raw: Any) -> dict:
    obj = unwrap(raw, {"claim_type", "retrieval_queries", "confidence"})
    entities = obj.get("entities")
    return {
        "claim_type": as_text(obj.get("claim_type"), limit=40).lower() or "undetermined",
        "confidence": as_float(obj.get("confidence"), 0.0),
        "secondary_types": as_str_list(obj.get("secondary_types"), limit=4),
        "entities": entities if isinstance(entities, dict) else {},
        "rationale": as_text(obj.get("rationale"), limit=1000),
        "retrieval_queries": as_str_list(obj.get("retrieval_queries"), limit=4),
    }


def plan(raw: Any) -> dict:
    obj = unwrap(raw, {"queries", "named_clauses_to_fetch", "modifiers_possible"})
    checked = obj.get("checked")
    queries = as_str_list(obj.get("queries"), limit=4)
    names = as_str_list(obj.get("named_clauses_to_fetch"), limit=4)
    # A planner that lists queries but forgets the flag clearly means "yes".
    modifiers = as_bool(obj.get("modifiers_possible"), default=bool(queries or names))
    return {
        "modifiers_possible": modifiers,
        "checked": checked if isinstance(checked, dict) else {},
        "named_clauses_to_fetch": names,
        "queries": queries,
        "reason": as_text(obj.get("reason"), limit=1000),
    }


_ROLES = {"grant", "restriction", "exclusion", "condition", "procedure", "definition"}


def draft(raw: Any) -> dict:
    obj = unwrap(raw, {"position", "citations", "summary"})
    citations = []
    for item in as_list(obj.get("citations")):
        if not isinstance(item, dict):
            continue
        clause_id = as_text(item.get("clause_id") or item.get("id"), limit=200)
        quote = as_text(item.get("quote") or item.get("text"), limit=1200)
        if not clause_id or not quote:
            continue
        role = as_text(item.get("role"), limit=20).lower()
        citations.append({"clause_id": clause_id, "quote": quote, "role": role if role in _ROLES else ""})
    return {
        "position": as_text(obj.get("position"), limit=40).lower().replace(" ", "_") or "undetermined",
        "summary": as_text(obj.get("summary"), limit=1000),
        "reasoning": as_text(obj.get("reasoning"), limit=4000),
        "governing_clauses": as_str_list(obj.get("governing_clauses"), limit=10),
        "modifying_clauses": as_str_list(obj.get("modifying_clauses"), limit=10),
        "open_questions": as_str_list(obj.get("open_questions"), limit=10),
        "confidence": as_float(obj.get("confidence"), 0.0),
        "citations": citations[:12],
    }


def verdict(raw: Any) -> dict:
    obj = unwrap(raw, {"verdict", "support_score", "unsupported_statements"})
    conflicts = []
    for pair in as_list(obj.get("clauses_in_conflict")):
        ids = as_str_list(pair, limit=4)
        if len(ids) >= 2:
            conflicts.append(ids)
    text = as_text(obj.get("verdict"), limit=20).lower()
    return {
        "verdict": "verified" if text in {"verified", "supported", "pass", "passed"} else "unsupported",
        "support_score": as_float(obj.get("support_score"), 0.0),
        "unsupported_statements": as_str_list(obj.get("unsupported_statements"), limit=10),
        "clauses_in_conflict": conflicts[:5],
        "notes": as_text(obj.get("notes"), limit=1000),
    }


def routing(raw: Any) -> dict:
    obj = unwrap(raw, {"queue", "reason", "priority"})
    priority = as_text(obj.get("priority"), limit=10).lower()
    return {
        "queue": as_text(obj.get("queue"), limit=40).lower().replace(" ", "_"),
        "reason": as_text(obj.get("reason"), limit=800),
        "priority": priority if priority in {"low", "normal", "high", "urgent"} else "normal",
        "target_first_touch_hours": as_int(obj.get("target_first_touch_hours"), 24, lo=1, hi=720),
        "triggered_rules": as_str_list(obj.get("triggered_rules"), limit=12),
    }
