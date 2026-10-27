"""Emit a valid FNOL body on stdout, for the shell scripts in this directory.

    python scripts/_make_fnol.py <reference> <declared-use> <description> <estimate>

Keeping the payload here rather than inline in the shell avoids two problems:
quoting a 200-character customer description inside a shell string, and letting
the demonstration scripts drift away from the contract. Anything this script
emits passes `fnol.v1` validation unless the caller deliberately breaks it
afterwards.
"""

from __future__ import annotations

import json
import sys


def build(reference: str, use: str, description: str, estimate: str) -> dict:
    return {
        "contract_version": "1.0.0",
        "claim_reference": reference,
        "policy_number": "IL/PC/2025/889012",
        "insurer_code": "ICICI_LOMBARD",
        "product_code": "PRIVATE_CAR_PACKAGE",
        "policy_start_date": "2025-04-01",
        "policy_end_date": "2026-03-31",
        "date_of_loss": "2025-11-04T19:40:00+05:30",
        "reported_at": "2025-11-04T21:05:00+05:30",
        "channel": "mobile_app",
        "reporter_phone": "+919845012345",
        "reporter_email": "policyholder@example.com",
        "vehicle": {
            "registration_number": "KA01MJ2023",
            "make": "Maruti Suzuki",
            "model": "Swift VXi",
            "year_of_manufacture": 2021,
            "insured_declared_value_inr": 620000.0,
            "odometer_km": 41200,
        },
        "driver": {
            "name": "A Policyholder",
            "age": 31,
            "licence_number": "KA0120190004567",
            "licence_valid_on_date_of_loss": True,
            "relationship_to_insured": "self",
            "holds_learner_licence_only": False,
        },
        "incident": {
            "description": description,
            "location": {"city": "Bengaluru", "state_code": "KA", "pincode": "560076"},
            "vehicle_use_at_time_of_loss": use,
            "vehicle_drivable": True,
            "third_party_involved": False,
            "third_party_injuries_reported": False,
            "occupant_injuries_reported": False,
            "police_report_filed": False,
            "fir_number": None,
            "vehicle_stolen": False,
            "airbags_deployed": False,
            "towed_from_scene": False,
            "estimated_repair_cost_inr": float(estimate),
            "witnesses_present": False,
        },
    }


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print(
            "usage: _make_fnol.py <reference> <declared-use> <description> <estimate>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(json.dumps(build(*sys.argv[1:5])))
