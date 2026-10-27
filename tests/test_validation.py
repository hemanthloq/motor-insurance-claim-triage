"""The ingestion boundary (F2, D1).

The property that matters most here is the last test in the file: a rejected
submission produces a quarantine record whose `agent_run_started` is False, and
there is no code path through `validate_submission` that can produce anything
else. Demonstration item 4 asks us to show that; this is where it is enforced.
"""

from __future__ import annotations

import copy

import pytest

from claimtriage.validation import validate_submission


def _validate(payload: dict):
    return validate_submission(payload, correlation_id="cid-test", client_id="pytest")


def test_valid_submission_passes(valid_fnol):
    outcome = _validate(valid_fnol)
    assert outcome.ok, [i.model_dump() for i in outcome.errors]
    assert outcome.fnol is not None
    assert outcome.quarantine is None
    assert outcome.errors == []


def test_registration_number_is_normalised(valid_fnol):
    payload = copy.deepcopy(valid_fnol)
    payload["vehicle"]["registration_number"] = "ka 01 mj 2023"
    outcome = _validate(payload)
    assert outcome.ok
    assert outcome.fnol.vehicle.registration_number == "KA01MJ2023"


@pytest.mark.parametrize(
    "mutate, expected_field",
    [
        (lambda p: p["vehicle"].update(registration_number="CAR-NUMBER-ONE"), "vehicle.registration_number"),
        (lambda p: p["vehicle"].update(insured_declared_value_inr=500.0), "vehicle.insured_declared_value_inr"),
        (lambda p: p["driver"].update(age=12), "driver.age"),
        (lambda p: p["incident"]["location"].update(state_code="ZZ"), "incident.location.state_code"),
        (lambda p: p["incident"]["location"].update(pincode="012345"), "incident.location.pincode"),
        (lambda p: p.update(channel="carrier_pigeon"), "channel"),
        (lambda p: p.update(reporter_phone="123"), "reporter_phone"),
        (lambda p: p["incident"].update(description="too short"), "incident.description"),
        (lambda p: p.update(policy_number="X/1"), "policy_number"),
    ],
)
def test_schema_range_and_categorical_rejections(valid_fnol, mutate, expected_field):
    payload = copy.deepcopy(valid_fnol)
    mutate(payload)
    outcome = _validate(payload)
    assert not outcome.ok
    assert outcome.quarantine.stage == "schema"
    assert any(i.field == expected_field for i in outcome.errors), [
        i.field for i in outcome.errors
    ]


def test_unknown_field_is_rejected(valid_fnol):
    """`extra="forbid"` is the contract. A caller sending a field we do not
    know about is a caller running against a different version of it."""
    payload = copy.deepcopy(valid_fnol)
    payload["settlement_amount_inr"] = 50000
    outcome = _validate(payload)
    assert not outcome.ok
    assert any("settlement_amount_inr" in i.field for i in outcome.errors)


@pytest.mark.parametrize(
    "mutate, rule",
    [
        (
            lambda p: p.update(date_of_loss="2026-05-02T10:00:00+05:30", reported_at="2026-05-02T12:00:00+05:30"),
            "loss_within_policy_period",
        ),
        (lambda p: p.update(policy_end_date="2025-03-01"), "policy_period_ordering"),
        (lambda p: p.update(reported_at="2025-11-04T10:00:00+05:30"), "loss_before_report"),
        (
            lambda p: p["incident"].update(police_report_filed=True, fir_number=None),
            "fir_required_for_police_report",
        ),
        (
            lambda p: p["incident"].update(vehicle_stolen=True, fir_number=None, estimated_repair_cost_inr=0.0),
            "fir_required_for_theft",
        ),
        (
            lambda p: p["incident"].update(third_party_involved=True, third_party_injuries_reported=True, fir_number=None),
            "fir_required_for_injury",
        ),
        (
            lambda p: p["incident"].update(third_party_involved=False, third_party_injuries_reported=True, fir_number="1/25"),
            "third_party_injury_consistency",
        ),
        (
            lambda p: p["incident"].update(vehicle_stolen=True, fir_number="1/25", estimated_repair_cost_inr=40000.0),
            "theft_excludes_repair_estimate",
        ),
        (lambda p: p["incident"].update(estimated_repair_cost_inr=2_500_000.0), "estimate_within_idv"),
        (lambda p: p["vehicle"].update(year_of_manufacture=2030), "manufacture_year_precedes_loss"),
    ],
)
def test_cross_field_rejections(valid_fnol, mutate, rule):
    payload = copy.deepcopy(valid_fnol)
    mutate(payload)
    outcome = _validate(payload)
    assert not outcome.ok, f"{rule} did not reject"
    assert outcome.quarantine.stage == "cross_field"
    assert any(i.rule == rule for i in outcome.errors), [i.rule for i in outcome.errors]


def test_future_timestamps_rejected(valid_fnol):
    payload = copy.deepcopy(valid_fnol)
    payload["policy_end_date"] = "2099-03-31"
    payload["date_of_loss"] = "2098-01-01T10:00:00+05:30"
    payload["reported_at"] = "2098-01-01T11:00:00+05:30"
    outcome = _validate(payload)
    assert not outcome.ok
    assert any(i.rule == "no_future_timestamps" for i in outcome.errors)


def test_restricted_use_is_a_warning_not_an_error(valid_fnol):
    """The validator flags the restricted use; it does not decide coverage.

    This is the division of labour the whole design rests on. If the validator
    rejected these claims, the agent would never see the case in section 4.1 --
    and a private car being used for delivery is a perfectly valid claim to
    triage, it just needs a different answer.
    """
    payload = copy.deepcopy(valid_fnol)
    payload["incident"]["vehicle_use_at_time_of_loss"] = "food_or_parcel_delivery"
    outcome = _validate(payload)
    assert outcome.ok
    assert any(w.rule == "commercial_use_flagged" for w in outcome.warnings)
    assert outcome.errors == []


def test_late_notification_is_a_warning(valid_fnol):
    payload = copy.deepcopy(valid_fnol)
    payload["date_of_loss"] = "2025-09-10T14:00:00+05:30"
    payload["reported_at"] = "2025-10-25T10:00:00+05:30"
    outcome = _validate(payload)
    assert outcome.ok
    assert any(w.rule == "late_notification" for w in outcome.warnings)


def test_possible_total_loss_is_a_warning(valid_fnol):
    payload = copy.deepcopy(valid_fnol)
    payload["incident"]["estimated_repair_cost_inr"] = 540_000.0
    outcome = _validate(payload)
    assert outcome.ok
    assert any(w.rule == "estimate_within_idv" and w.severity == "warning" for w in outcome.warnings)


def test_quarantine_record_is_complete_and_serialisable(valid_fnol):
    payload = copy.deepcopy(valid_fnol)
    payload["vehicle"]["registration_number"] = "!!!"
    outcome = _validate(payload)
    record = outcome.quarantine
    assert record is not None
    assert record.quarantine_id.startswith("qr-")
    assert record.correlation_id == "cid-test"
    assert record.client_id == "pytest"
    assert record.issues
    assert record.raw_payload
    # Quarantined bodies get stored and shipped over HTTP, so they must encode.
    import json

    json.dumps(record.model_dump(mode="json"))


def test_rejection_never_reports_a_started_agent_run(valid_fnol):
    """Demonstration item 4, as an invariant rather than a screenshot."""
    broken = [
        {},
        {"claim_reference": "x"},
        {**valid_fnol, "date_of_loss": "not-a-date"},
        {**valid_fnol, "policy_end_date": "2020-01-01"},
    ]
    for payload in broken:
        outcome = _validate(payload)
        assert not outcome.ok
        assert outcome.quarantine.agent_run_started is False


def test_malformed_body_does_not_raise():
    """The boundary must never raise -- it returns an outcome, always."""
    for payload in [{}, {"a": [1, 2, 3]}, {"incident": "not an object"}]:
        outcome = _validate(payload)
        assert not outcome.ok
        assert outcome.quarantine is not None
