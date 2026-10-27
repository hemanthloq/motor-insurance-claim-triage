"""FNOL, triage result and feedback contracts -- version 1.0.0.

Design notes
------------
* Every field that a downstream stage reads is declared here. If an agent needs
  a value, it must come through this contract, not out of the free text.
* `fnol.incident.description` is the only unbounded free-text field that reaches
  a model. It is length-capped here and guardrailed in `claimtriage.guardrails`.
* Validation is layered: pydantic covers schema, type, range and category;
  `claimtriage.validation` adds the cross-field rules that pydantic cannot
  express cleanly and produces field-level errors for the quarantine record.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# -----------------------------------------------------------------------------
# Enumerations -- the categorical validation surface
# -----------------------------------------------------------------------------


class Channel(str, Enum):
    call_centre = "call_centre"
    mobile_app = "mobile_app"
    web_form = "web_form"
    branch = "branch"
    broker = "broker"


class VehicleUse(str, Enum):
    """Declared use of the vehicle at the moment of loss.

    This is the structured counterpart of the free-text hint that the section
    4.1 case turns on. It is deliberately a *declared* value, not a derived one:
    the agent must still reconcile it against the description and against the
    policy's limitations-as-to-use clause.
    """

    private_social_domestic = "private_social_domestic"
    commuting_to_work = "commuting_to_work"
    commercial_goods_carriage = "commercial_goods_carriage"
    commercial_passenger_hire = "commercial_passenger_hire"
    food_or_parcel_delivery = "food_or_parcel_delivery"
    ride_hailing = "ride_hailing"
    driving_tuition = "driving_tuition"
    motor_trade = "motor_trade"
    racing_or_speed_testing = "racing_or_speed_testing"
    not_stated = "not_stated"


class ClaimType(str, Enum):
    own_damage = "own_damage"
    third_party = "third_party"
    theft = "theft"
    total_loss = "total_loss"
    undetermined = "undetermined"


class CoveragePosition(str, Enum):
    likely_covered = "likely_covered"
    likely_not_covered = "likely_not_covered"
    partially_covered = "partially_covered"
    undetermined = "undetermined"


class RoutingQueue(str, Enum):
    fast_track = "fast_track"
    surveyor_assignment = "surveyor_assignment"
    investigation = "investigation"
    human_escalation = "human_escalation"


class RunStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    escalated = "escalated"
    failed = "failed"
    rejected = "rejected"


class EscalationReason(str, Enum):
    weak_retrieval = "weak_retrieval"
    conflicting_clauses = "conflicting_clauses"
    outside_corpus = "outside_corpus"
    verification_failed = "verification_failed"
    loop_budget_exhausted = "loop_budget_exhausted"
    provider_unavailable = "provider_unavailable"
    guardrail_block = "guardrail_block"
    policy_requires_human = "policy_requires_human"


class FeedbackVerdict(str, Enum):
    satisfactory = "satisfactory"
    unsatisfactory = "unsatisfactory"


# -----------------------------------------------------------------------------
# FNOL request  (contract: fnol.v1)
# -----------------------------------------------------------------------------

REGISTRATION_RE = re.compile(r"^[A-Z]{2}[ -]?\d{1,2}[ -]?[A-Z]{0,3}[ -]?\d{1,4}$")
POLICY_RE = re.compile(r"^[A-Z0-9][A-Z0-9/\-]{5,39}$")
PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")

INDIAN_STATES = [
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "DN", "GA", "GJ", "HP",
    "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP", "MZ", "NL",
    "OD", "PB", "PY", "RJ", "SK", "TG", "TN", "TR", "TS", "UK", "UP", "WB",
]


class VehicleDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration_number: str = Field(
        ..., min_length=4, max_length=16, examples=["KA01MJ2023"]
    )
    make: str = Field(..., min_length=1, max_length=40)
    model_name: str = Field(..., min_length=1, max_length=60, alias="model")
    year_of_manufacture: int = Field(..., ge=1950, le=2100)
    insured_declared_value_inr: float = Field(..., ge=10_000, le=50_000_000)
    odometer_km: int | None = Field(default=None, ge=0, le=2_000_000)

    @field_validator("registration_number")
    @classmethod
    def _registration_shape(cls, value: str) -> str:
        normalised = value.upper().strip()
        if not REGISTRATION_RE.match(normalised):
            raise ValueError(
                "registration_number must look like an Indian plate, e.g. KA01MJ2023"
            )
        return normalised.replace(" ", "").replace("-", "")


class DriverDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    age: int = Field(..., ge=16, le=100)
    licence_number: str = Field(..., min_length=5, max_length=24)
    licence_valid_on_date_of_loss: bool
    relationship_to_insured: Literal[
        "self", "spouse", "family", "employee", "friend", "paid_driver", "other"
    ]
    holds_learner_licence_only: bool = False


class IncidentLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str = Field(..., min_length=1, max_length=80)
    state_code: str = Field(..., min_length=2, max_length=2)
    pincode: str = Field(..., min_length=6, max_length=6)

    @field_validator("state_code")
    @classmethod
    def _known_state(cls, value: str) -> str:
        code = value.upper()
        if code not in INDIAN_STATES:
            raise ValueError(f"state_code must be one of {', '.join(INDIAN_STATES)}")
        return code

    @field_validator("pincode")
    @classmethod
    def _pincode_shape(cls, value: str) -> str:
        if not PINCODE_RE.match(value):
            raise ValueError("pincode must be six digits and must not start with 0")
        return value


class IncidentDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        ...,
        min_length=10,
        max_length=4000,
        description=(
            "Free text as given by the customer. This is the only unbounded "
            "field that reaches a model; it is guardrailed before it does."
        ),
    )
    location: IncidentLocation
    vehicle_use_at_time_of_loss: VehicleUse = VehicleUse.not_stated
    vehicle_drivable: bool
    third_party_involved: bool
    third_party_injuries_reported: bool = False
    occupant_injuries_reported: bool = False
    police_report_filed: bool = False
    fir_number: str | None = Field(default=None, max_length=40)
    vehicle_stolen: bool = False
    airbags_deployed: bool = False
    towed_from_scene: bool = False
    estimated_repair_cost_inr: float = Field(..., ge=0, le=50_000_000)
    witnesses_present: bool = False


class FNOLRequest(BaseModel):
    """Contract `fnol.v1` -- the only shape the service will accept (F1, D1)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    contract_version: Literal["1.0.0"] = "1.0.0"
    claim_reference: str = Field(
        ...,
        min_length=6,
        max_length=48,
        description=(
            "Caller-supplied idempotency key. Two submissions with the same "
            "reference and the same body return the same run."
        ),
    )
    policy_number: str = Field(..., min_length=6, max_length=40)
    insurer_code: str = Field(..., min_length=2, max_length=24)
    product_code: str = Field(..., min_length=2, max_length=32)
    policy_start_date: date
    policy_end_date: date
    date_of_loss: datetime
    reported_at: datetime
    channel: Channel
    reporter_phone: str = Field(..., min_length=10, max_length=16)
    reporter_email: EmailStr | None = None
    vehicle: VehicleDetails
    driver: DriverDetails
    incident: IncidentDetails

    @field_validator("policy_number")
    @classmethod
    def _policy_shape(cls, value: str) -> str:
        normalised = value.upper().strip()
        if not POLICY_RE.match(normalised):
            raise ValueError(
                "policy_number must be 6-40 characters of A-Z, 0-9, '-' or '/'"
            )
        return normalised

    @field_validator("reporter_phone")
    @classmethod
    def _phone_shape(cls, value: str) -> str:
        compact = value.replace(" ", "")
        if not PHONE_RE.match(compact):
            raise ValueError("reporter_phone must be 10-15 digits, optional leading +")
        return compact

    @field_validator("date_of_loss", "reported_at")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# -----------------------------------------------------------------------------
# Validation / quarantine
# -----------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    rule: str
    message: str
    severity: Literal["error", "warning"] = "error"
    observed: Any | None = None


class QuarantineRecord(BaseModel):
    """What is written when a submission is rejected at the boundary (D1, F2)."""

    model_config = ConfigDict(extra="forbid")

    quarantine_id: str
    correlation_id: str
    received_at: datetime
    claim_reference: str | None = None
    client_id: str | None = None
    stage: Literal["schema", "cross_field", "guardrail", "rate_limit"]
    issues: list[ValidationIssue]
    raw_payload: dict[str, Any]
    agent_run_started: Literal[False] = False


# -----------------------------------------------------------------------------
# Triage result  (contract: triage_result.v1)
# -----------------------------------------------------------------------------


class Citation(BaseModel):
    """A clause-level citation. `quote` must be a verbatim span of `clause_id`."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    document_id: str
    document_title: str
    section_path: str
    page: int | None = None
    quote: str
    relevance: float = Field(..., ge=0.0, le=1.0)
    role: Literal["grant", "restriction", "exclusion", "condition", "procedure", "definition"]
    resolves: bool = Field(
        default=True,
        description="True when the quote was found verbatim in the retrieved chunk.",
    )


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_type: ClaimType
    confidence: float = Field(..., ge=0.0, le=1.0)
    secondary_types: list[ClaimType] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class CoverageDetermination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: CoveragePosition
    summary: str
    reasoning: str
    governing_clauses: list[str] = Field(default_factory=list)
    modifying_clauses: list[str] = Field(
        default_factory=list,
        description="Restrictions or exclusions that qualify the governing clauses.",
    )
    open_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RoutingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue: RoutingQueue
    reason: str
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    target_first_touch_hours: int = Field(default=24, ge=1, le=720)
    triggered_rules: list[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    """A3 -- the answer is checked against the retrieved text before it leaves."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["verified", "unsupported", "not_run"]
    support_score: float = Field(default=0.0, ge=0.0, le=1.0)
    citations_checked: int = 0
    citations_resolved: int = 0
    unsupported_statements: list[str] = Field(default_factory=list)
    attempts: int = 0
    notes: str = ""


class StepRecord(BaseModel):
    """One row of the reconstructible run trace (D5)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    agent: str
    step: str
    started_at: datetime
    duration_ms: float
    outcome: str
    prompt_version: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    tool: str | None = None
    tool_query: str | None = None
    retrieved_clause_ids: list[str] = Field(default_factory=list)
    top_score: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class EscalationNotice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = False
    reason: EscalationReason | None = None
    explanation: str = ""
    assigned_to: str = "senior_claims_handler"


class CostBreakdown(BaseModel):
    """L5. Tokens and spend, attributed per step as well as per run.

    `tokens_per_step` is here alongside `per_step` spend because on a free tier
    every spend figure is 0.00 and "the largest single cost driver" would be
    unanswerable from money alone. Tokens are the invariant unit: they identify
    the same step as the driver whatever the price list says, and they are what
    a rate limit is denominated in.
    """

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    per_step: dict[str, float] = Field(
        default_factory=dict, description="USD per step, at the prices in force."
    )
    tokens_per_step: dict[str, int] = Field(default_factory=dict)
    largest_driver: str | None = Field(
        default=None,
        description=(
            "The step that dominated the run: by spend when any price is "
            "configured, otherwise by tokens."
        ),
    )
    largest_driver_basis: Literal["cost", "tokens", "none"] = "none"


class TriageResult(BaseModel):
    """Contract `triage_result.v1` -- what the caller eventually receives."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    correlation_id: str
    claim_reference: str
    status: RunStatus
    submitted_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float = 0.0

    classification: Classification | None = None
    coverage: CoverageDetermination | None = None
    routing: RoutingDecision | None = None
    verification: VerificationReport = Field(default_factory=lambda: VerificationReport(verdict="not_run"))
    escalation: EscalationNotice = Field(default_factory=EscalationNotice)
    citations: list[Citation] = Field(default_factory=list)

    steps: list[StepRecord] = Field(default_factory=list)
    tool_calls: int = 0
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    index_version: str | None = None
    trace_id: str | None = None
    served_from_cache: bool = Field(
        default=False,
        description=(
            "True when this answer was replayed from the whole-run cache rather "
            "than computed. A cached run makes no model calls, so its `cost` is "
            "zero; `cached_from_run_id` points at the run that paid for it."
        ),
    )
    cached_from_run_id: str | None = None
    degraded: bool = False
    degradation_path: list[str] = Field(default_factory=list)
    guardrail_events: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None

    advisory_notice: str = (
        "Advisory only. This service does not approve, reject or value a claim. "
        "A human claims handler makes the decision."
    )


class RunAcceptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    correlation_id: str
    claim_reference: str
    status: RunStatus
    status_url: str
    result_url: str
    queued_at: datetime
    estimated_seconds: float


class RunStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    correlation_id: str
    status: RunStatus
    current_step: str | None = None
    steps_completed: int = 0
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_seconds: float = 0.0
    result_available: bool = False
    result_url: str


# -----------------------------------------------------------------------------
# Feedback  (contract: feedback.v1)
# -----------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """Contract `feedback.v1` (L6)."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0.0"] = "1.0.0"
    run_id: str = Field(..., min_length=4, max_length=64)
    verdict: FeedbackVerdict
    reason: str = Field(..., min_length=3, max_length=1000)
    reviewer: str = Field(default="anonymous", max_length=80)
    corrected_queue: RoutingQueue | None = None
    corrected_position: CoveragePosition | None = None
