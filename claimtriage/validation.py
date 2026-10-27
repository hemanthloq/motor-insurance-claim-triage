"""The ingestion boundary (F2, D1).

Four layers of validation run here, in order, *before* a run identifier is ever
issued and long before an agent starts:

  1. schema + type   -- pydantic parses the body against `fnol.v1`
  2. range           -- numeric and date bounds, declared on the model
  3. categorical     -- enums and the state-code list, declared on the model
  4. cross-field     -- the rules in `CROSS_FIELD_RULES` below, which pydantic
                        cannot express cleanly because they span sub-objects

A failure at any layer produces a `QuarantineRecord` carrying every field-level
error, which is persisted and returned with HTTP 422. `agent_run_started` on
that record is typed `Literal[False]`: the quarantine path has no branch that
can start a run, which is what demonstration item 4 asks us to show.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from pydantic import ValidationError

from .contracts.models import (
    FNOLRequest,
    QuarantineRecord,
    ValidationIssue,
    VehicleUse,
)

#: Clock skew we tolerate on `reported_at` before calling it a future timestamp.
FUTURE_SKEW = timedelta(minutes=5)

#: Reporting later than this is accepted but flagged; the policy conditions
#: require immediate notice, which the agent will pick up as a condition issue.
LATE_NOTICE_DAYS = 30


class ValidationOutcome:
    """Result of running the boundary over one payload."""

    __slots__ = ("fnol", "issues", "quarantine")

    def __init__(
        self,
        fnol: FNOLRequest | None,
        issues: list[ValidationIssue],
        quarantine: QuarantineRecord | None,
    ) -> None:
        self.fnol = fnol
        self.issues = issues
        self.quarantine = quarantine

    @property
    def ok(self) -> bool:
        return self.fnol is not None and self.quarantine is None

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]


# -----------------------------------------------------------------------------
# Cross-field rules
# -----------------------------------------------------------------------------

Rule = Callable[[FNOLRequest], Iterable[ValidationIssue]]
CROSS_FIELD_RULES: list[tuple[str, Rule]] = []


def rule(name: str) -> Callable[[Rule], Rule]:
    def register(fn: Rule) -> Rule:
        CROSS_FIELD_RULES.append((name, fn))
        return fn

    return register


@rule("policy_period_ordering")
def _policy_period_ordering(fnol: FNOLRequest):
    if fnol.policy_end_date <= fnol.policy_start_date:
        yield ValidationIssue(
            field="policy_end_date",
            rule="policy_period_ordering",
            message="policy_end_date must fall after policy_start_date",
            observed=str(fnol.policy_end_date),
        )


@rule("loss_within_policy_period")
def _loss_within_policy_period(fnol: FNOLRequest):
    loss_day = fnol.date_of_loss.date()
    if loss_day < fnol.policy_start_date or loss_day > fnol.policy_end_date:
        yield ValidationIssue(
            field="date_of_loss",
            rule="loss_within_policy_period",
            message=(
                "date_of_loss falls outside the policy period "
                f"{fnol.policy_start_date}..{fnol.policy_end_date}; "
                "there is no cover to assess"
            ),
            observed=fnol.date_of_loss.isoformat(),
        )


@rule("loss_before_report")
def _loss_before_report(fnol: FNOLRequest):
    if fnol.reported_at < fnol.date_of_loss:
        yield ValidationIssue(
            field="reported_at",
            rule="loss_before_report",
            message="reported_at cannot precede date_of_loss",
            observed=fnol.reported_at.isoformat(),
        )


@rule("no_future_timestamps")
def _no_future_timestamps(fnol: FNOLRequest):
    now = datetime.now(timezone.utc)
    for field, value in (
        ("date_of_loss", fnol.date_of_loss),
        ("reported_at", fnol.reported_at),
    ):
        if value > now + FUTURE_SKEW:
            yield ValidationIssue(
                field=field,
                rule="no_future_timestamps",
                message=f"{field} is in the future",
                observed=value.isoformat(),
            )


@rule("late_notification")
def _late_notification(fnol: FNOLRequest):
    delay = fnol.reported_at - fnol.date_of_loss
    if delay > timedelta(days=LATE_NOTICE_DAYS):
        yield ValidationIssue(
            field="reported_at",
            rule="late_notification",
            message=(
                f"loss reported {delay.days} days after the event; the policy "
                "conditions require immediate notice"
            ),
            severity="warning",
            observed=delay.days,
        )


@rule("manufacture_year_precedes_loss")
def _manufacture_year(fnol: FNOLRequest):
    if fnol.vehicle.year_of_manufacture > fnol.date_of_loss.year:
        yield ValidationIssue(
            field="vehicle.year_of_manufacture",
            rule="manufacture_year_precedes_loss",
            message="vehicle cannot have been manufactured after the loss",
            observed=fnol.vehicle.year_of_manufacture,
        )


@rule("fir_required_for_police_report")
def _fir_for_police_report(fnol: FNOLRequest):
    inc = fnol.incident
    if inc.police_report_filed and not (inc.fir_number or "").strip():
        yield ValidationIssue(
            field="incident.fir_number",
            rule="fir_required_for_police_report",
            message="police_report_filed is true but no fir_number was supplied",
        )


@rule("fir_required_for_theft")
def _fir_for_theft(fnol: FNOLRequest):
    inc = fnol.incident
    if inc.vehicle_stolen and not (inc.fir_number or "").strip():
        yield ValidationIssue(
            field="incident.fir_number",
            rule="fir_required_for_theft",
            message="a theft claim cannot be triaged without an FIR number",
        )


@rule("fir_required_for_injury")
def _fir_for_injury(fnol: FNOLRequest):
    inc = fnol.incident
    injured = inc.third_party_injuries_reported or inc.occupant_injuries_reported
    if injured and not (inc.fir_number or "").strip():
        yield ValidationIssue(
            field="incident.fir_number",
            rule="fir_required_for_injury",
            message="injuries were reported but no FIR number was supplied",
        )


@rule("third_party_injury_consistency")
def _tp_injury_consistency(fnol: FNOLRequest):
    inc = fnol.incident
    if inc.third_party_injuries_reported and not inc.third_party_involved:
        yield ValidationIssue(
            field="incident.third_party_injuries_reported",
            rule="third_party_injury_consistency",
            message=(
                "third_party_injuries_reported is true while "
                "third_party_involved is false"
            ),
        )


@rule("theft_excludes_repair_estimate")
def _theft_vs_repair(fnol: FNOLRequest):
    inc = fnol.incident
    if inc.vehicle_stolen and inc.estimated_repair_cost_inr > 0:
        yield ValidationIssue(
            field="incident.estimated_repair_cost_inr",
            rule="theft_excludes_repair_estimate",
            message=(
                "a repair estimate was given for a vehicle reported stolen; "
                "one of the two fields is wrong"
            ),
        )


@rule("estimate_within_idv")
def _estimate_within_idv(fnol: FNOLRequest):
    estimate = fnol.incident.estimated_repair_cost_inr
    idv = fnol.vehicle.insured_declared_value_inr
    if estimate > idv * 3:
        yield ValidationIssue(
            field="incident.estimated_repair_cost_inr",
            rule="estimate_within_idv",
            message=(
                "repair estimate exceeds three times the insured declared value; "
                "this is implausible rather than a total loss"
            ),
            observed=estimate,
        )
    elif estimate > idv * 0.75:
        yield ValidationIssue(
            field="incident.estimated_repair_cost_inr",
            rule="estimate_within_idv",
            message=(
                "repair estimate exceeds 75% of the insured declared value; "
                "treat as a possible total loss"
            ),
            severity="warning",
            observed=estimate,
        )


@rule("learner_licence_consistency")
def _learner_licence(fnol: FNOLRequest):
    drv = fnol.driver
    if drv.holds_learner_licence_only and drv.licence_valid_on_date_of_loss is False:
        yield ValidationIssue(
            field="driver.licence_valid_on_date_of_loss",
            rule="learner_licence_consistency",
            message=(
                "driver held only a learner licence and it was not valid on the "
                "date of loss; the driver's clause cannot be satisfied"
            ),
            severity="warning",
        )


@rule("commercial_use_flagged")
def _commercial_use(fnol: FNOLRequest):
    """Not an error -- a signal.

    A private car policy restricts the purposes the vehicle may be used for.
    When the declared use is one of the restricted purposes we attach a warning
    so the agent is obliged to reconcile it against the limitations clause. The
    determination itself is the agent's job, not the validator's.
    """
    restricted = {
        VehicleUse.commercial_goods_carriage,
        VehicleUse.commercial_passenger_hire,
        VehicleUse.food_or_parcel_delivery,
        VehicleUse.ride_hailing,
        VehicleUse.motor_trade,
        VehicleUse.racing_or_speed_testing,
        VehicleUse.driving_tuition,
    }
    if fnol.incident.vehicle_use_at_time_of_loss in restricted:
        yield ValidationIssue(
            field="incident.vehicle_use_at_time_of_loss",
            rule="commercial_use_flagged",
            message=(
                "declared use may fall outside the permitted use of a private "
                "car policy; the limitations-as-to-use clause must be examined"
            ),
            severity="warning",
            observed=fnol.incident.vehicle_use_at_time_of_loss.value,
        )


@rule("undrivable_but_trivial_estimate")
def _undrivable_trivial(fnol: FNOLRequest):
    inc = fnol.incident
    if not inc.vehicle_drivable and inc.estimated_repair_cost_inr < 5_000:
        yield ValidationIssue(
            field="incident.estimated_repair_cost_inr",
            rule="undrivable_but_trivial_estimate",
            message=(
                "vehicle reported undrivable with a repair estimate under "
                "INR 5,000; the two are inconsistent"
            ),
            severity="warning",
        )


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------


def _pydantic_issues(exc: ValidationError) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<body>"
        issues.append(
            ValidationIssue(
                field=location,
                rule=err["type"],
                message=err["msg"],
                observed=_safe(err.get("input")),
            )
        )
    return issues


def _safe(value: Any) -> Any:
    """Keep quarantine records JSON-serialisable and bounded in size."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 500:
            return value[:500] + "...[truncated]"
        return value
    return str(value)[:500]


def validate_submission(
    payload: dict[str, Any],
    *,
    correlation_id: str,
    client_id: str | None = None,
) -> ValidationOutcome:
    """Run the full boundary. Never raises; always returns an outcome."""
    received_at = datetime.now(timezone.utc)
    claim_reference = None
    if isinstance(payload, dict):
        raw_ref = payload.get("claim_reference")
        if isinstance(raw_ref, str):
            claim_reference = raw_ref[:48]

    # Layers 1-3.
    try:
        fnol = FNOLRequest.model_validate(payload)
    except ValidationError as exc:
        issues = _pydantic_issues(exc)
        return ValidationOutcome(
            fnol=None,
            issues=issues,
            quarantine=QuarantineRecord(
                quarantine_id=f"qr-{uuid.uuid4().hex[:16]}",
                correlation_id=correlation_id,
                received_at=received_at,
                claim_reference=claim_reference,
                client_id=client_id,
                stage="schema",
                issues=issues,
                raw_payload=_shrink(payload),
            ),
        )

    # Layer 4.
    issues: list[ValidationIssue] = []
    for _name, check in CROSS_FIELD_RULES:
        issues.extend(check(fnol))

    blocking = [i for i in issues if i.severity == "error"]
    if blocking:
        return ValidationOutcome(
            fnol=None,
            issues=issues,
            quarantine=QuarantineRecord(
                quarantine_id=f"qr-{uuid.uuid4().hex[:16]}",
                correlation_id=correlation_id,
                received_at=received_at,
                claim_reference=fnol.claim_reference,
                client_id=client_id,
                stage="cross_field",
                issues=issues,
                raw_payload=_shrink(payload),
            ),
        )

    return ValidationOutcome(fnol=fnol, issues=issues, quarantine=None)


def _shrink(payload: Any, limit: int = 20_000) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"_non_object_body": _safe(payload)}
    import json

    try:
        encoded = json.dumps(payload, default=str)
    except Exception:
        return {"_unserialisable_body": True}
    if len(encoded) <= limit:
        return json.loads(encoded)
    return {"_truncated": True, "_prefix": encoded[:limit]}


def guardrail_quarantine(
    payload: dict[str, Any],
    *,
    correlation_id: str,
    issues: list[ValidationIssue],
    client_id: str | None = None,
    stage: str = "guardrail",
) -> QuarantineRecord:
    return QuarantineRecord(
        quarantine_id=f"qr-{uuid.uuid4().hex[:16]}",
        correlation_id=correlation_id,
        received_at=datetime.now(timezone.utc),
        claim_reference=(payload or {}).get("claim_reference"),
        client_id=client_id,
        stage=stage,  # type: ignore[arg-type]
        issues=issues,
        raw_payload=_shrink(payload),
    )
