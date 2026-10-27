"""Deterministic routing signals.

Computed from the validated structured fields, never from the free text and
never by a model. Two reasons.

First, these are the facts the business already knows with certainty -- is there
a third party, was anyone hurt, is the car drivable, what is the estimate
against the IDV. Asking a model to re-derive them from prose would be slower,
more expensive and less reliable than reading the contract field.

Second, they give the routing stage a floor. The model chooses the queue, but a
model that wants to fast-track a claim with a bodily injury is overruled: the
`enforce` function below raises the queue to the most cautious one that the
signals demand. A model is allowed to be more careful than the rules, never
less.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts.models import RoutingQueue

#: Mandatory investigation triggers. These are not advisory.
INVESTIGATION_TRIGGERS = {
    "theft_claim": "a theft claim requires investigation before assessment",
    "restricted_use_declared": "declared use may fall outside the permitted use of a private car policy",
    "driver_not_licensed": "the driver did not hold a valid licence on the date of loss",
    "learner_driver": "the driver held a learner licence only",
    "internally_inconsistent": "the account is internally inconsistent",
    "coverage_position_negative": "the coverage position is adverse to the claimant",
    "coverage_undetermined": "the coverage position could not be determined from the wording",
}

SURVEYOR_TRIGGERS = {
    "possible_total_loss": "the estimate approaches the insured declared value",
    "vehicle_undrivable": "the vehicle is not drivable and must be inspected",
    "high_value": "the estimate exceeds the fast-track ceiling",
    "injury_reported": "an injury was reported",
    "third_party_property": "third party property damage must be assessed",
}

#: Fast-track ceiling. A policy decision, not a technical one; stated here so
#: it is reviewable in one place rather than embedded in a prompt.
FAST_TRACK_CEILING_INR = 50_000.0
TOTAL_LOSS_RATIO = 0.70

RESTRICTED_USES = {
    "commercial_goods_carriage",
    "commercial_passenger_hire",
    "food_or_parcel_delivery",
    "ride_hailing",
    "motor_trade",
    "racing_or_speed_testing",
    "driving_tuition",
}


@dataclass
class RoutingSignals:
    investigation: dict[str, str] = field(default_factory=dict)
    surveyor: dict[str, str] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def floor(self) -> RoutingQueue:
        if self.investigation:
            return RoutingQueue.investigation
        if self.surveyor:
            return RoutingQueue.surveyor_assignment
        return RoutingQueue.fast_track

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigation_triggers": self.investigation,
            "surveyor_triggers": self.surveyor,
            "facts": self.facts,
            "minimum_queue": self.floor.value,
        }


_SEVERITY = {
    RoutingQueue.fast_track: 0,
    RoutingQueue.surveyor_assignment: 1,
    RoutingQueue.investigation: 2,
    RoutingQueue.human_escalation: 3,
}


def compute(claim: dict[str, Any], *, coverage_position: str | None = None) -> RoutingSignals:
    incident = claim.get("incident") or {}
    vehicle = claim.get("vehicle") or {}
    driver = claim.get("driver") or {}

    estimate = float(incident.get("estimated_repair_cost_inr") or 0.0)
    idv = float(vehicle.get("insured_declared_value_inr") or 0.0)
    use = incident.get("vehicle_use_at_time_of_loss") or "not_stated"

    signals = RoutingSignals(
        facts={
            "estimated_repair_cost_inr": estimate,
            "insured_declared_value_inr": idv,
            "estimate_to_idv_ratio": round(estimate / idv, 3) if idv else None,
            "declared_use": use,
            "vehicle_drivable": incident.get("vehicle_drivable"),
            "third_party_involved": incident.get("third_party_involved"),
            "injuries": bool(incident.get("third_party_injuries_reported"))
            or bool(incident.get("occupant_injuries_reported")),
            "fir_number": incident.get("fir_number"),
            "licence_valid": driver.get("licence_valid_on_date_of_loss"),
        }
    )

    if incident.get("vehicle_stolen"):
        signals.investigation["theft_claim"] = INVESTIGATION_TRIGGERS["theft_claim"]
    if use in RESTRICTED_USES:
        signals.investigation["restricted_use_declared"] = (
            f"{INVESTIGATION_TRIGGERS['restricted_use_declared']} (declared: {use})"
        )
    if driver.get("licence_valid_on_date_of_loss") is False:
        signals.investigation["driver_not_licensed"] = INVESTIGATION_TRIGGERS["driver_not_licensed"]
    if driver.get("holds_learner_licence_only"):
        signals.investigation["learner_driver"] = INVESTIGATION_TRIGGERS["learner_driver"]
    if coverage_position == "likely_not_covered":
        signals.investigation["coverage_position_negative"] = INVESTIGATION_TRIGGERS[
            "coverage_position_negative"
        ]
    if coverage_position == "undetermined":
        signals.investigation["coverage_undetermined"] = INVESTIGATION_TRIGGERS[
            "coverage_undetermined"
        ]

    if idv and estimate >= idv * TOTAL_LOSS_RATIO:
        signals.surveyor["possible_total_loss"] = SURVEYOR_TRIGGERS["possible_total_loss"]
    if incident.get("vehicle_drivable") is False:
        signals.surveyor["vehicle_undrivable"] = SURVEYOR_TRIGGERS["vehicle_undrivable"]
    if estimate > FAST_TRACK_CEILING_INR:
        signals.surveyor["high_value"] = (
            f"{SURVEYOR_TRIGGERS['high_value']} of INR {FAST_TRACK_CEILING_INR:,.0f}"
        )
    if signals.facts["injuries"]:
        signals.surveyor["injury_reported"] = SURVEYOR_TRIGGERS["injury_reported"]
    if incident.get("third_party_involved"):
        signals.surveyor["third_party_property"] = SURVEYOR_TRIGGERS["third_party_property"]

    return signals


def enforce(chosen: RoutingQueue, signals: RoutingSignals) -> tuple[RoutingQueue, list[str]]:
    """Raise the model's choice to the floor the signals demand.

    Returns the queue actually used and the names of any rules that overrode
    the model, so the override is visible in the result rather than silent.
    """
    floor = signals.floor
    if _SEVERITY[chosen] >= _SEVERITY[floor]:
        return chosen, []
    overrides = list(signals.investigation) if floor == RoutingQueue.investigation else list(
        signals.surveyor
    )
    return floor, overrides
