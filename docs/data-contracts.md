# Data contracts (F1, F2, D1)

Four contracts, version 1.0.0. The pydantic models in
`claimtriage/contracts/models.py` are the single source of truth; JSON Schema is
generated from them and served live, so a consumer can never be reading a schema
that has drifted from the code that enforces it.

```bash
curl -s localhost:8080/contracts            # the index
curl -s localhost:8080/contracts/fnol       # JSON Schema, draft 2020-12
docker compose run --rm tools export-contracts --outdir /app/contracts
```

| contract | direction | endpoint |
|---|---|---|
| `fnol.v1` | in | `POST /v1/claims` |
| `triage_result.v1` | out | `GET /v1/runs/{id}/result`, and the optional callback |
| `feedback.v1` | in | `POST /v1/feedback` |
| `quarantine.v1` | out | `422` bodies, `GET /v1/quarantine/{id}` |

`extra="forbid"` on every model. A caller sending a field we do not know about is
a caller running against a different version of the contract, and accepting it
silently is how a `settlement_amount_inr` field ends up being ignored by a system
that is not allowed to have one.

---

## `fnol.v1` — the only shape accepted

```jsonc
{
  "contract_version": "1.0.0",
  "claim_reference": "FNOL-2025-0098231",    // caller's idempotency key
  "policy_number": "IL/PC/2025/889012",
  "insurer_code": "ICICI_LOMBARD",
  "product_code": "PRIVATE_CAR_PACKAGE",
  "policy_start_date": "2025-04-01",
  "policy_end_date": "2026-03-31",
  "date_of_loss": "2025-11-04T19:40:00+05:30",
  "reported_at": "2025-11-04T21:05:00+05:30",
  "channel": "mobile_app",                   // call_centre|mobile_app|web_form|branch|broker
  "reporter_phone": "+919845012345",
  "reporter_email": "policyholder@example.com",
  "vehicle": {
    "registration_number": "KA01MJ2023",
    "make": "Maruti Suzuki",
    "model": "Swift VXi",
    "year_of_manufacture": 2021,
    "insured_declared_value_inr": 620000.0,
    "odometer_km": 41200
  },
  "driver": {
    "name": "A Policyholder", "age": 31,
    "licence_number": "KA0120190004567",
    "licence_valid_on_date_of_loss": true,
    "relationship_to_insured": "self",       // self|spouse|family|employee|friend|paid_driver|other
    "holds_learner_licence_only": false
  },
  "incident": {
    "description": "...",                    // the only unbounded free-text field
    "location": { "city": "Bengaluru", "state_code": "KA", "pincode": "560076" },
    "vehicle_use_at_time_of_loss": "food_or_parcel_delivery",
    "vehicle_drivable": true,
    "third_party_involved": false,
    "third_party_injuries_reported": false,
    "occupant_injuries_reported": false,
    "police_report_filed": false,
    "fir_number": null,
    "vehicle_stolen": false,
    "airbags_deployed": false,
    "towed_from_scene": false,
    "estimated_repair_cost_inr": 48000.0,
    "witnesses_present": false
  }
}
```

### Why `vehicle_use_at_time_of_loss` is a structured enum

This field is the structured counterpart of the thing the section 4.1 case turns
on, and making it an enum rather than leaving it to the free text is a deliberate
design decision with a specific consequence.

It is a **declared** value, not a derived one. The agent must still reconcile it
against the description — the Intake Analyst is asked explicitly whether they
agree and reports `use_discrepancy` when they do not. A customer who says
"private use" in the dropdown and "I was on a delivery run" in the free text has
told us something important, and the contract is shaped so that discrepancy is
visible rather than resolved silently in favour of whichever the system happened
to read.

The nine values are: `private_social_domestic`, `commuting_to_work`,
`commercial_goods_carriage`, `commercial_passenger_hire`,
`food_or_parcel_delivery`, `ride_hailing`, `driving_tuition`, `motor_trade`,
`racing_or_speed_testing`, `not_stated`. Seven of them map onto purposes the
policy's limitations clause restricts; `commuting_to_work` deliberately does
not, and `eval/dataset.yaml::S02` is the control that checks the system knows the
difference.

### The four validation layers

In order, in `claimtriage/validation.py`. All four run **before a run
identifier is issued.**

**1. Schema and type.** pydantic parses the body. Patterns:
`registration_number` against an Indian plate shape (and normalised to
`KA01MJ2023` form), `policy_number` to 6–40 characters of `A-Z0-9/-`,
`reporter_phone` to 10–15 digits, `pincode` to six digits not starting with zero.

**2. Range.** `insured_declared_value_inr` ∈ [10 000, 50 000 000];
`estimated_repair_cost_inr` ∈ [0, 50 000 000]; `driver.age` ∈ [16, 100];
`year_of_manufacture` ∈ [1950, 2100]; `description` 10–4000 characters;
`odometer_km` ∈ [0, 2 000 000].

**3. Categorical.** Every enum above, plus `state_code` against the 38 Indian
state and union-territory codes.

**4. Cross-field.** Fourteen rules that span sub-objects and so cannot be
expressed cleanly on the model. Each is a named function in a registry, which is
what lets `eval/dataset.yaml` assert that a specific rule fired.

| rule | severity | what it catches |
|---|---|---|
| `policy_period_ordering` | error | end date not after start date |
| `loss_within_policy_period` | error | **no cover to assess** |
| `loss_before_report` | error | reported before it happened |
| `no_future_timestamps` | error | either timestamp in the future (5 min skew) |
| `manufacture_year_precedes_loss` | error | car built after the crash |
| `fir_required_for_police_report` | error | police report claimed, no FIR number |
| `fir_required_for_theft` | error | theft cannot be triaged without an FIR |
| `fir_required_for_injury` | error | injuries reported, no FIR |
| `third_party_injury_consistency` | error | injuries to a third party that was not involved |
| `theft_excludes_repair_estimate` | error | a repair estimate for a stolen car |
| `estimate_within_idv` | error > 3× IDV | implausible rather than a total loss |
| `estimate_within_idv` | warning > 75% | possible total loss |
| `late_notification` | warning > 30 days | the conditions require immediate notice |
| `learner_licence_consistency` | warning | learner licence and not valid on the day |
| `commercial_use_flagged` | **warning** | declared use may fall outside the permitted use |
| `undrivable_but_trivial_estimate` | warning | undrivable with a sub-₹5,000 estimate |

### Why `commercial_use_flagged` is a warning and not an error

This is the most consequential line in the validator, and it would be easy to get
backwards.

A private car used for food delivery is a **perfectly valid claim to triage.** It
may well be payable — whether a delivery run is "hire or reward" or "carriage of
goods" turns on facts the FNOL does not contain. If the validator rejected it,
the agent would never see the case in section 4.1 at all, and the system would be
making a coverage determination in a regex.

So the validator flags it and hands it on. The determination is the agent's job,
grounded in the clause, with citations. The division of labour is: **the
validator decides whether the submission is well-formed; the agent decides what
the policy says.** `tests/test_validation.py::test_restricted_use_is_a_warning_not_an_error`
pins it.

Warnings are passed to the agent in the graph state and shown in the
`coverage_analysis` prompt's `flags` block, so the model knows what the boundary
noticed.

---

## `quarantine.v1` — what a rejection produces

```jsonc
{
  "quarantine_id": "qr-90e58ee445a146ec",
  "correlation_id": "cid-6694214227e34216",
  "received_at": "2026-10-14T10:04:51.812Z",
  "claim_reference": "FNOL-2025-0098231",
  "client_id": "mobile-app-prod",
  "stage": "cross_field",              // schema|cross_field|guardrail|rate_limit
  "issues": [
    { "field": "date_of_loss",
      "rule": "loss_within_policy_period",
      "message": "date_of_loss falls outside the policy period 2025-04-01..2026-03-31; there is no cover to assess",
      "severity": "error",
      "observed": "2026-05-02T10:00:00+05:30" }
  ],
  "raw_payload": { ... },
  "agent_run_started": false
}
```

`agent_run_started` is typed `Literal[False]`. It is not a field that happens to
be false — it is a field that **cannot** be true, because there is no code path
from the quarantine branch to a run. That is how "a malformed request must not
start an agent run" is made checkable by reading one function rather than by
inspecting logs.

`raw_payload` is truncated at 20 KB and every value is coerced to something
JSON-serialisable, because quarantine records get stored and shipped over HTTP
and a record that cannot be encoded is a record that is lost.

Returned with `422`, retrievable at `GET /v1/quarantine/{id}`, listed at
`GET /v1/quarantine`, counted by
`triage_quarantined_records_total{reason}`.

---

## `triage_result.v1` — what comes back

The full shape is in `GET /contracts/triage_result`. The parts worth calling out:

```jsonc
{
  "status": "completed",               // queued|running|completed|escalated|failed|rejected
  "classification": { "claim_type": "own_damage", "confidence": 0.82,
                      "entities": { "declared_use_consistent_with_description": false, ... } },
  "coverage": { "position": "likely_not_covered",   // likely_covered|likely_not_covered|partially_covered|undetermined
                "summary": "...", "reasoning": "...",
                "governing_clauses": ["..."], "modifying_clauses": ["..."],
                "open_questions": ["Was the delivery carried out for reward?"],
                "confidence": 0.74 },
  "routing": { "queue": "investigation",            // fast_track|surveyor_assignment|investigation|human_escalation
               "reason": "...", "priority": "high",
               "target_first_touch_hours": 8, "triggered_rules": ["restricted_use_declared"] },
  "verification": { "verdict": "verified", "support_score": 0.93,
                    "citations_checked": 3, "citations_resolved": 3,
                    "unsupported_statements": [], "attempts": 1 },
  "escalation": { "required": false, "reason": null, "assigned_to": "senior_claims_handler" },
  "citations": [ { "clause_id": "...", "document_title": "...", "section_path": "...",
                   "page": 19, "quote": "...", "role": "restriction",
                   "relevance": 0.98, "resolves": true } ],
  "steps": [ { "index": 1, "agent": "intake_analyst", "step": "intake_classification",
               "duration_ms": 404.2, "outcome": "ok",
               "prompt_version": "intake_classification@v2+b2c20418",
               "prompt_tokens": 753, "completion_tokens": 101, "cost_usd": 0.0,
               "tool": null, "tool_query": null, "retrieved_clause_ids": [], "top_score": null } ],
  "prompt_versions": { "coverage_analysis": "coverage_analysis@v2+1fd579ef", ... },
  "cost": { "total_tokens": 15390, "tokens_per_step": { ... },
            "largest_driver": "coverage_analysis", "largest_driver_basis": "tokens" },
  "index_version": "e4956b50fed7ea94",
  "served_from_cache": false,
  "degraded": false,
  "degradation_path": [],
  "advisory_notice": "Advisory only. This service does not approve, reject or value a claim..."
}
```

Three guarantees the contract makes:

1. **`citations[].resolves` is always `true`.** Citations that did not resolve are
   dropped in `runner._assemble` and counted in `verification`. The caller never
   sees an unresolved citation.
2. **`status: "completed"` implies `verification.verdict: "verified"`.** An
   unverified answer is never returned as complete.
3. **`routing` is always populated**, including on `escalated` and `failed`. A
   claim never leaves the pipeline with no queue.

`advisory_notice` is a required field with a fixed default rather than something
the client is expected to know. The service is advisory by design and the
response says so on every single run.

---

## `feedback.v1`

```jsonc
{
  "contract_version": "1.0.0",
  "run_id": "run-04dcec4594184df5",
  "verdict": "unsatisfactory",          // satisfactory|unsatisfactory
  "reason": "The citation did not address the declared use.",
  "reviewer": "handler-42",
  "corrected_queue": "investigation",   // optional
  "corrected_position": "likely_not_covered"
}
```

`corrected_queue` is the most valuable field in the system. A handler disagreeing
with the routing is the clearest quality signal available, and
`GET /v1/feedback/summary` surfaces exactly those disagreements as a
`corrections` list — the input to the next prompt revision.

---

## Headers

| header | direction | |
|---|---|---|
| `X-Correlation-ID` | in, out | generated if absent; follows the claim through every service and into the logs, metrics and trace |
| `X-Client-Id` | in | rate-limit bucket; falls back to `X-Forwarded-For`, then the peer address |
| `X-Callback-Url` | in | optional; gets a POST of the `triage_result` when the run finishes |
| `Location` | out | `/v1/runs/{id}` on a 202 |
| `X-RateLimit-Remaining` | out | |
| `Retry-After` | out | on a 429 |

`X-Callback-Url` is a header rather than a field on the FNOL deliberately: it is
transport concern, not claim data, and putting it in the body would mean the
idempotency key and the run cache both had to know to ignore it.

---

## Versioning

`contract_version` is a required literal on every contract, so a request from a
client built against a future version fails at the boundary with a clear error
rather than being partially understood.

The intended path for a breaking change is a new path prefix (`/v2/claims`) with
both served in parallel, because a claims intake has call-centre software and
mobile apps in the field that cannot be upgraded simultaneously. Nothing in the
code assumes `v1` is the only version — the contract models are a package and the
routes carry the prefix.
