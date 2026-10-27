"""Published data contracts for every external interface (D1, F1).

The pydantic models in `models.py` are the single source of truth. JSON Schema
for each one is generated from them by `python -m claimtriage.cli export-contracts`
and served live at `GET /contracts/{name}`, so a consumer can never be looking at
a schema that has drifted from the code that enforces it.
"""

from .models import (  # noqa: F401
    Channel,
    ClaimType,
    CoveragePosition,
    Citation,
    DriverDetails,
    EscalationReason,
    FeedbackRequest,
    FeedbackVerdict,
    FNOLRequest,
    IncidentDetails,
    QuarantineRecord,
    RoutingQueue,
    RunAcceptedResponse,
    RunStatus,
    RunStatusResponse,
    StepRecord,
    TriageResult,
    ValidationIssue,
    VehicleDetails,
    VehicleUse,
    VerificationReport,
)

CONTRACT_VERSION = "1.0.0"
