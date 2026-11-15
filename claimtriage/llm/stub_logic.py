"""Deterministic stand-in for a model (D6).

The load test has to measure *our* system, not a free tier's rate limiter, so
the model is replaced by something that answers in a fixed, realistic time. But
a stub that returns one constant string is no use: the graph would short-circuit
at the first parse failure and the load test would measure a code path nobody
runs in anger.

So this stub is rule-based rather than constant. It reads the step tag that each
prompt carries, pulls the clause identifiers out of the context block it was
given, and returns a well-formed response for that step which is consistent with
what it was shown. Under load the graph therefore executes the same number of
steps, the same tool calls and the same verification work as it does against a
real provider.

What it is not: a model. It cannot read a policy clause. Every evaluation number
in this repo (L2, L3) comes from a real provider, and the eval harness refuses
to write a report when the stub is active.

Two consumers share this module: `StubProvider` (in-process, for tests and for a
no-network demonstration) and `services/llm_stub` (over HTTP, for Locust).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..retrieval.chunker import ROLE_RULES

STEP_TAG = re.compile(r"\[\[STEP:([a-z_]+)\]\]")
CLAUSE_TAG = re.compile(r"\[([a-z0-9_]+::[a-z0-9\-]+::\d{4})\]")

#: Keyword -> claim type. Crude on purpose: the stub's job is to be fast and
#: stable, not clever.
_TYPE_RULES: list[tuple[str, str]] = [
    (r"\bstolen|theft|missing vehicle\b", "theft"),
    (r"\btotal loss|burnt out|written off|submerged\b", "total_loss"),
    (r"\bthird part(y|ies)|pedestrian|other vehicle|collided with another\b", "third_party"),
    (r"\bdent|scratch|bumper|bonnet|windscreen|skidded|pothole|hail\b", "own_damage"),
]

#: Read the *declared* use out of the claim summary rather than scanning the
#: whole prompt. The prompt also contains the retrieved policy text, which is
#: full of phrases like "hire or reward" and "carriage of goods" -- scanning all
#: of it made every scenario look like a restricted-use case, including the
#: ones written specifically as controls for that mistake.
_DECLARED_USE = re.compile(r'"declared_use_at_time_of_loss"\s*:\s*"([a-z_]+)"')
_RESTRICTED_USES = {
    "commercial_goods_carriage",
    "commercial_passenger_hire",
    "food_or_parcel_delivery",
    "ride_hailing",
    "motor_trade",
    "racing_or_speed_testing",
    "driving_tuition",
}
#: Fallback for prompts that carry no structured summary (the intake step sees
#: the description on its own). Anchored on customer phrasing, not policy
#: vocabulary.
_RESTRICTED_DESCRIPTION = re.compile(
    r"food (delivery|order)|delivery run|parcel delivery|ride booking app|"
    r"paid driving lesson|track day|carrying cartons|stock for my shop",
    re.IGNORECASE,
)


def _is_restricted(prompt: str) -> bool:
    declared = _DECLARED_USE.search(prompt)
    if declared:
        return declared.group(1) in _RESTRICTED_USES
    return bool(_RESTRICTED_DESCRIPTION.search(prompt))


def detect_step(prompt: str) -> str:
    match = STEP_TAG.search(prompt)
    return match.group(1) if match else "unknown"


def _clause_ids(prompt: str, limit: int = 4) -> list[str]:
    seen: list[str] = []
    for cid in CLAUSE_TAG.findall(prompt):
        if cid not in seen:
            seen.append(cid)
        if len(seen) >= limit:
            break
    return seen


#: The context block's header line: "[clause_id] (role, title, section, page N)".
_HEADER = re.compile(r"\[([a-z0-9_]+::[a-z0-9\-]+::\d{4})\]\s*\((\w+),\s*(.*?),\s*page\s*\d+\)")


def _clauses_by_role(prompt: str) -> list[tuple[str, str, str]]:
    """(clause_id, role, header text) for each passage, in context order."""
    out, seen = [], set()
    for match in _HEADER.finditer(prompt):
        cid = match.group(1)
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, match.group(2).lower(), match.group(3).lower()))
    return out


def _pick(clauses, role: str, prefer: str = "", exclude=()) -> str | None:
    """First clause of a role, preferring one whose heading mentions `prefer`."""
    pool = [c for c in clauses if c[1] == role and c[0] not in exclude]
    for cid, _role, header in pool:
        if prefer and prefer in header:
            return cid
    return pool[0][0] if pool else None


def _choose_citations(prompt: str, restricted: bool) -> list[str]:
    """Which clauses the stub cites.

    It picks by the role tag and section heading printed in the context block
    -- it does not read the clauses, and nothing here should be mistaken for
    reasoning. What it does buy is a trace that shows the retrieval mechanism
    working: the clauses `find_named_clause` fetched are the ones that appear in
    the answer, instead of whichever three ids happened to come first. The
    first version did take the first three, and for the section 4.1 case it
    cited a clause about paying the premium in EMI instalments.
    """
    clauses = _clauses_by_role(prompt)
    if not clauses:
        return _clause_ids(prompt, limit=3)
    chosen: list[str] = []
    if restricted:
        order = [("restriction", "limitations as to use"), ("exclusion", "exception"),
                 ("grant", "loss of or damage")]
    else:
        # A covered position rests on the grant and the conditions it is
        # subject to. Citing an exclusion here put a "BUT -- takes cover away"
        # card under a LIKELY COVERED stamp, quoting a clause about the
        # insurer's option to repair.
        order = [("grant", "loss of or damage"), ("condition", "condition"), ("condition", "")]
    for role, prefer in order:
        cid = _pick(clauses, role, prefer, exclude=chosen)
        if cid:
            chosen.append(cid)
    for cid, _role, _header in clauses:  # top up from what is left
        if len(chosen) >= 3:
            break
        if cid not in chosen:
            chosen.append(cid)
    return chosen[:3]


def _quote_for(prompt: str, clause_id: str, role: str | None = None, words: int = 18) -> str:
    """Lift a verbatim span from the context so citations actually resolve.

    The context block puts the clause metadata on the same line as the marker
    and the clause text on the line below, so the span has to start after that
    first newline. Getting this wrong is not harmless: the quote then fails the
    deterministic resolution check, every run goes round the retry loop three
    times and escalates, and the load test measures the escalation path instead
    of the happy path.
    """
    marker = f"[{clause_id}]"
    position = prompt.find(marker)
    if position == -1:
        return ""
    tail = prompt[position + len(marker) :]
    newline = tail.find("\n")
    body = tail[newline + 1 :] if newline != -1 else tail
    # stop at the blank line that separates this passage from the next
    body = body.split("\n\n", 1)[0]
    return " ".join(_role_sentence(body, role).split()[:words])


def _role_sentence(body: str, role: str | None) -> str:
    """The passage from the first sentence that carries the clause's role on.

    Quoting the first words of every clause put "This Policy and the Schedule
    shall be read together" on a card badged EXCLUSION -- true to the text,
    and no use to a handler. The role came from the chunker's own rules, so
    the sentence those rules matched is the one worth quoting.
    """
    pattern = dict(ROLE_RULES).get(role or "")
    if pattern is None:
        return body
    for match in re.finditer(r"[^.;:]+(?:[.;:]|$)", body):
        if pattern.search(match.group(0)) and len(match.group(0).split()) >= 4:
            return body[match.start():].lstrip()
    return body


_CUSTOMER_TEXT = re.compile(r"<<<CUSTOMER_TEXT\s*(.*?)\s*CUSTOMER_TEXT", re.DOTALL)


def _claim_type(prompt: str) -> str:
    """Classify from the customer's words and the structured flags only.

    Scanning the whole prompt -- as an earlier version did -- reads the
    structured summary too, which always contains the key `"vehicle_stolen"`.
    The word "stolen" therefore matched for every claim, and the stub labelled a
    food-delivery kerb strike a theft. The same mistake had already been fixed
    once for restricted use (see `_is_restricted`) and was missed here.
    """
    if re.search(r'"vehicle_stolen"\s*:\s*true', prompt):
        return "theft"
    block = _CUSTOMER_TEXT.search(prompt)
    text = (block.group(1) if block else "").lower()
    for pattern, claim_type in _TYPE_RULES:
        if claim_type == "theft":
            continue  # theft comes from the structured flag above, not from prose
        if re.search(pattern, text):
            return claim_type
    if re.search(r'"third_party_involved"\s*:\s*true', prompt):
        return "third_party"
    return "own_damage"


def respond(prompt: str) -> str:
    """Return the JSON body a real model would have returned for this step."""
    step = detect_step(prompt)
    clauses = _clause_ids(prompt)
    restricted = _is_restricted(prompt)

    if step == "intake_classification":
        claim_type = _claim_type(prompt)
        return json.dumps(
            {
                "claim_type": claim_type,
                "confidence": 0.82,
                "secondary_types": ["third_party"] if claim_type == "own_damage" else [],
                "entities": {
                    "declared_use_consistent_with_description": not restricted,
                    "severity_hint": "moderate",
                },
                "rationale": (
                    "Stub classifier: keyword rules over the incident description "
                    "and the structured fields."
                ),
                "retrieval_queries": [
                    "loss of or damage to the vehicle insured accidental external means",
                    "general exceptions applicable to all sections",
                ],
            }
        )

    if step == "query_formulation":
        queries = [
            "limitations as to use hire or reward carriage of goods",
            "general exceptions vehicle used otherwise than in accordance with limitations as to use",
        ] if restricted else [
            "conditions precedent to liability notice of claim",
            "deductible compulsory excess applicable to own damage",
        ]
        return json.dumps(
            {
                "modifiers_possible": True,
                "named_clauses_to_fetch": (
                    ["Limitations as to Use", "General Exceptions"] if restricted else ["Conditions"]
                ),
                "queries": queries,
                "reason": "Stub planner: the grant may be qualified elsewhere in the wording.",
            }
        )

    if step == "coverage_analysis":
        cited = _choose_citations(prompt, restricted)
        clauses = cited  # governing / modifying lists below follow the same order
        roles = {cid: role for cid, role, _header in _clauses_by_role(prompt)}
        citations = [
            {"clause_id": cid, "quote": quote, "role": ""}
            for cid in cited
            if (quote := _quote_for(prompt, cid, roles.get(cid)))
        ]
        if restricted:
            return json.dumps(
                {
                    "position": "likely_not_covered",
                    "summary": (
                        "The vehicle appears to have been in use for a purpose the "
                        "policy does not permit at the time of the loss."
                    ),
                    "reasoning": (
                        "Stub reasoner: the own damage grant is qualified by the "
                        "general exception for use otherwise than in accordance with "
                        "the limitations as to use, and the declared use falls within "
                        "the restricted purposes."
                    ),
                    # restricted order is (restriction, exclusion, grant): the
                    # grant governs and the other two modify it
                    "governing_clauses": clauses[2:3],
                    "modifying_clauses": clauses[0:2],
                    "open_questions": [
                        "Was the delivery carried out for reward or as a personal favour?"
                    ],
                    "confidence": 0.74,
                    "citations": citations,
                }
            )
        return json.dumps(
            {
                "position": "likely_covered",
                "summary": "The loss appears to fall within the own damage cover.",
                "reasoning": (
                    "Stub reasoner: the grant applies and no retrieved restriction or "
                    "exclusion displaces it."
                ),
                "governing_clauses": clauses[:1],
                "modifying_clauses": [],
                "open_questions": [],
                "confidence": 0.71,
                "citations": citations,
            }
        )

    if step == "verification":
        return json.dumps(
            {
                "verdict": "verified" if clauses else "unsupported",
                "support_score": 0.86 if clauses else 0.2,
                "unsupported_statements": [] if clauses else ["no citation resolved"],
                "notes": "Stub verifier: checked each citation against the context block.",
            }
        )

    if step == "routing":
        if restricted:
            return json.dumps(
                {
                    "queue": "investigation",
                    "reason": (
                        "Declared use may fall outside the permitted use of the policy; "
                        "a handler must establish the facts of the journey."
                    ),
                    "priority": "high",
                    "target_first_touch_hours": 8,
                }
            )
        return json.dumps(
            {
                "queue": "fast_track",
                # The stub does not read the claim's facts, so it must not
                # assert them: "Low value, drivable vehicle" was printed for an
                # undrivable car with a Rs 2.6 lakh estimate.
                "reason": "Stub router: no restricted-use signal, so it proposes the fast track; "
                          "the mandatory rules decide whether the claim stays there.",
                "priority": "normal",
                "target_first_touch_hours": 24,
            }
        )

    return json.dumps({"stub": True, "step": step, "note": "no rule for this step"})


def usage_for(prompt: str, completion: str) -> dict[str, int]:
    return {
        "prompt_tokens": max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(completion) // 4),
        "total_tokens": max(2, (len(prompt) + len(completion)) // 4),
    }


def openai_envelope(prompt: str, model: str = "stub-1") -> dict[str, Any]:
    body = respond(prompt)
    usage = usage_for(prompt, body)
    return {
        "id": "stubcmpl-0",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": body}, "finish_reason": "stop"}
        ],
        "usage": usage,
    }
