"""Guardrails (L4).

Three surfaces, because an agent has three and a pipeline has fewer:

  input   -- the customer's free text, before it reaches any model
  tool    -- text pulled out of the corpus, before it is pasted into a prompt
  output  -- the determination, before it reaches the caller

The tool surface is the one people forget. An agent holds tools; text that
reaches it can try to use them. Our corpus is a published insurer document and
is not hostile today, but the same code path will one day index a claimant's
uploaded email, and the defence has to exist before that happens rather than
after.

Why rules and not a classifier
------------------------------
Every guardrail here is deterministic and explainable. That is a deliberate
trade. A model-based detector would catch more paraphrases, but it would add a
model call per field to a pipeline that already makes several per run, on a free
tier with a low rate limit, and it would make the reason for a block
unauditable -- "the classifier said 0.71" is not something a compliance reviewer
can act on. These rules catch the direct-instruction family, which is what
actually appears in claims text, and every block names the exact pattern that
fired. The limitation is stated in the report rather than hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .config import get_settings
from .logging_setup import get_logger
from .telemetry import guardrail_events

log = get_logger("guardrails")


@dataclass
class GuardrailFinding:
    kind: str  # injection | toxicity | unauthorised_output | pii_leak
    surface: str  # input | tool | output
    pattern: str
    excerpt: str
    score: float
    action: str  # block | neutralise | flag

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "surface": self.surface,
            "pattern": self.pattern,
            "excerpt": self.excerpt[:200],
            "score": round(self.score, 3),
            "action": self.action,
        }


@dataclass
class GuardrailResult:
    allowed: bool
    text: str
    findings: list[GuardrailFinding] = field(default_factory=list)

    @property
    def triggered(self) -> bool:
        return bool(self.findings)


# -----------------------------------------------------------------------------
# Prompt injection
# -----------------------------------------------------------------------------

INJECTION_PATTERNS: list[tuple[str, str, float]] = [
    ("override_instructions", r"\b(ignore|disregard|forget|override)\b[^.\n]{0,30}\b(previous|prior|above|earlier|all|your)\b[^.\n]{0,20}\b(instruction|prompt|rule|direction|context)", 0.95),
    ("role_reassignment", r"\b(you are now|from now on,? you|act as|pretend to be|roleplay as|new persona)\b", 0.8),
    ("system_prompt_exfiltration", r"\b(reveal|print|show|repeat|output|disclose)\b[^.\n]{0,30}\b(system prompt|your instructions|initial prompt|the prompt above)", 0.95),
    ("authority_claim", r"\b(as (the|an) (administrator|admin|developer|auditor|underwriter|supervisor)|i am (the|an) (administrator|admin|developer))\b", 0.75),
    ("determination_forcing", r"\b(approve|authorise|authorize|settle|pay out|sanction)\b[^.\n]{0,25}\b(this|the)\b[^.\n]{0,15}\bclaim\b", 0.85),
    ("mandatory_output", r"\b(you must (say|output|return|respond|answer)|always (say|respond with|output)|respond only with)\b", 0.7),
    ("delimiter_injection", r"(<<<\s*(system|instruction)|\[\[\s*system\s*\]\]|###\s*system\s*:|\bsystem\s*:\s*you are)", 0.85),
    # Deliberately narrow. An earlier, looser version of this pattern matched
    # "EXCLUSION OF DAMAGE WHILE IN USE AS A TOOL OF TRADE" -- a real heading in
    # the policy wording -- and fired on almost every retrieval.
    ("tool_coercion", r"\b(call|invoke|execute)\s+(the\s+)?(tool|function|command|endpoint)\b|\buse\s+the\s+\w+\s+tool\b", 0.65),
    ("guardrail_disable", r"\b(disable|turn off|bypass|skip)\b[^.\n]{0,25}\b(guardrail|safety|filter|validation|verification|check)", 0.9),
    ("encoded_payload", r"\b(base64|rot13|decode the following|eval\(|exec\()\b", 0.65),
]

_COMPILED_INJECTION = [
    (name, re.compile(pattern, re.IGNORECASE), weight)
    for name, pattern, weight in INJECTION_PATTERNS
]


def scan_injection(text: str, *, surface: str = "input") -> list[GuardrailFinding]:
    findings = []
    for name, pattern, weight in _COMPILED_INJECTION:
        match = pattern.search(text or "")
        if match:
            findings.append(
                GuardrailFinding(
                    kind="injection",
                    surface=surface,
                    pattern=name,
                    excerpt=_window(text, match.start(), match.end()),
                    score=weight,
                    action="block" if weight >= get_settings().injection_threshold else "flag",
                )
            )
    return findings


# -----------------------------------------------------------------------------
# Toxicity
# -----------------------------------------------------------------------------

#: Severity-weighted lexicon. Slurs and threats block; ordinary profanity in a
#: claim description does not -- people who have just crashed their car swear,
#: and refusing to triage their claim for it would be a worse failure than the
#: one we are guarding against.
TOXICITY_LEXICON: list[tuple[str, str, float]] = [
    ("threat_of_violence", r"\b(i('| a)?m going to |i will )(kill|shoot|stab|burn|hurt|harm)\b", 0.95),
    ("threat_generic", r"\b(kill|murder|shoot|bomb|burn down)\b[^.\n]{0,20}\b(you|your family|the office|the branch)\b", 0.9),
    ("self_harm", r"\b(kill myself|end my life|suicide)\b", 0.9),
    ("sexual_harassment", r"\b(rape|molest)\b", 0.95),
    ("slur_marker", r"\b(bastard|swine)\b[^.\n]{0,15}\b(agent|staff|officer|handler)\b", 0.7),
    ("abuse_directed", r"\b(you|your staff) (are|is) (a |an )?(idiot|moron|fool|useless|incompetent|corrupt|thief|thieves|fraud)", 0.55),
    ("profanity_mild", r"\b(damn|bloody|hell|crap)\b", 0.1),
]

_COMPILED_TOXICITY = [
    (name, re.compile(pattern, re.IGNORECASE), weight)
    for name, pattern, weight in TOXICITY_LEXICON
]


def scan_toxicity(text: str, *, surface: str = "input") -> list[GuardrailFinding]:
    threshold = get_settings().toxicity_threshold
    findings = []
    for name, pattern, weight in _COMPILED_TOXICITY:
        match = pattern.search(text or "")
        if match:
            findings.append(
                GuardrailFinding(
                    kind="toxicity",
                    surface=surface,
                    pattern=name,
                    excerpt=_window(text, match.start(), match.end()),
                    score=weight,
                    action="block" if weight >= threshold else "flag",
                )
            )
    return findings


# -----------------------------------------------------------------------------
# Output filtering -- the determination the service is not authorised to make
# -----------------------------------------------------------------------------

UNAUTHORISED_OUTPUT: list[tuple[str, str]] = [
    ("claim_approved", r"\b(claim (is|has been) (approved|accepted|admitted|sanctioned)|we (will|shall) (pay|settle|reimburse))\b"),
    ("claim_rejected", r"\b(claim (is|has been) (rejected|repudiated|denied|declined)|we (will not|won't|shall not) pay)\b"),
    ("settlement_amount", r"\b(settlement amount|amount payable|we will pay you|payable to you)\b[^.\n]{0,30}(rs\.?|inr|₹)\s?[\d,]+"),
    ("currency_determination", r"(rs\.?|inr|₹)\s?[\d,]{4,}\s*(will be|is|shall be)\s*(paid|settled|reimbursed|approved)"),
    ("instruction_to_claimant", r"\b(you (should|must) (now )?(collect|receive|expect) (the )?(payment|settlement|cheque))\b"),
    ("legal_advice", r"\b(you have a (strong|weak) (legal )?case|you should sue|take legal action against)\b"),
]

_COMPILED_OUTPUT = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in UNAUTHORISED_OUTPUT]

#: The advisory language the service *is* permitted to use. Present so that a
#: reviewer can see the boundary from both sides.
PERMITTED_LANGUAGE = (
    "likely covered",
    "likely not covered",
    "appears to fall within",
    "appears to fall outside",
    "a handler must establish",
    "recommend",
    "advisory",
)


def scan_output(text: str) -> list[GuardrailFinding]:
    findings = []
    for name, pattern in _COMPILED_OUTPUT:
        match = pattern.search(text or "")
        if match:
            findings.append(
                GuardrailFinding(
                    kind="unauthorised_output",
                    surface="output",
                    pattern=name,
                    excerpt=_window(text, match.start(), match.end()),
                    score=1.0,
                    action="block",
                )
            )
    return findings


# -----------------------------------------------------------------------------
# Neutralisation
# -----------------------------------------------------------------------------

_NEUTRALISE = [
    (re.compile(r"(?i)\b(ignore|disregard|forget|override)\b"), "[redacted-directive]"),
    (re.compile(r"(?i)\b(you are now|act as|pretend to be)\b"), "[redacted-directive]"),
    (re.compile(r"(?i)(<<<|>>>|\[\[|\]\])"), " "),
    (re.compile(r"(?i)\b(system|assistant)\s*:"), "reported-text:"),
]


def neutralise(text: str) -> str:
    """Defang a field we are going to show a model anyway.

    Used for text we must not drop -- the customer's description is the whole
    substance of the claim -- where the finding was a flag rather than a block.
    """
    out = text
    for pattern, replacement in _NEUTRALISE:
        out = pattern.sub(replacement, out)
    return out


# -----------------------------------------------------------------------------
# Entry points
# -----------------------------------------------------------------------------


def check_input(text: str, *, surface: str = "input") -> GuardrailResult:
    if not get_settings().guardrails_enabled:
        return GuardrailResult(allowed=True, text=text)
    findings = scan_injection(text, surface=surface) + scan_toxicity(text, surface=surface)
    _emit(findings)
    blocking = [f for f in findings if f.action == "block"]
    if blocking:
        return GuardrailResult(allowed=False, text=text, findings=findings)
    return GuardrailResult(
        allowed=True,
        text=neutralise(text) if findings else text,
        findings=findings,
    )


#: Above this score a retrieved passage is dropped rather than shown to the model.
TOOL_SURFACE_DROP_THRESHOLD = 0.9


def check_tool_output(text: str) -> GuardrailResult:
    """Corpus text on its way into a prompt.

    This surface does **not** rewrite the text, and that is a correctness
    requirement rather than a preference. The model is asked to quote these
    passages verbatim, and the verifier then checks each quote
    character-for-character against the clause as stored in the index. If the
    guardrail silently rewrote the copy the model saw, every quote taken from a
    rewritten passage would fail to resolve, the run would loop through all
    three search strategies and escalate, and the cause would look like a
    retrieval problem rather than a guardrail one.

    So the behaviour is: record every finding, and for a high-confidence
    finding drop the whole passage (`allowed=False`) so it never reaches the
    prompt at all. Dropping is safe -- a passage the agent never sees is a
    passage it cannot cite -- whereas altering is not.
    """
    if not get_settings().guardrails_enabled:
        return GuardrailResult(allowed=True, text=text)
    findings = scan_injection(text, surface="tool")
    drop = any(f.score >= TOOL_SURFACE_DROP_THRESHOLD for f in findings)
    for finding in findings:
        finding.action = "drop_passage" if drop else "flag"
    _emit(findings)
    return GuardrailResult(allowed=not drop, text=text, findings=findings)


def check_output(text: str) -> GuardrailResult:
    if not get_settings().guardrails_enabled:
        return GuardrailResult(allowed=True, text=text)
    findings = scan_output(text) + scan_toxicity(text, surface="output")
    _emit(findings)
    blocking = [f for f in findings if f.action == "block"]
    return GuardrailResult(allowed=not blocking, text=text, findings=findings)


def check_fnol(payload: dict) -> GuardrailResult:
    """Every free-text field on the FNOL that can reach a model."""
    description = ((payload.get("incident") or {}).get("description")) or ""
    return check_input(description, surface="input")


def _emit(findings: Iterable[GuardrailFinding]) -> None:
    for finding in findings:
        guardrail_events.labels(kind=finding.kind, surface=finding.surface).inc()
        log.warning(
            "guardrail_triggered",
            kind=finding.kind,
            surface=finding.surface,
            pattern=finding.pattern,
            score=finding.score,
            action=finding.action,
            excerpt=finding.excerpt[:120],
        )


def _window(text: str, start: int, end: int, pad: int = 60) -> str:
    return (text or "")[max(0, start - pad) : min(len(text or ""), end + pad)].strip()
