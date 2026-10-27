# Service level objective (D6)

> **This target was declared before any measurement was taken.** It is written
> into `.env.example` as `SLO_P95_SECONDS=45`, into the Prometheus alerting
> rule `SLOBreach` in `ops/prometheus/rules.yml`, and into the Grafana panel
> threshold, all of which were committed before the load test was first run.
> The measured numbers are in [load-test.md](load-test.md), in a separate file,
> added afterwards.

## The objective

**p95 of a complete run, from submission to final result, must be at or under
45 seconds.** Measured on the `triage_run_duration_seconds` histogram, which is
observed once per run in `claimtriage/agents/runner.py` and covers the whole
graph: intake, retrieval, analysis, verification, any retry loop, and routing.

Two secondary objectives, which exist to stop the headline number being gamed:

| | objective | why |
|---|---|---|
| p50 complete run | ≤ 20 s | a p95 that is met only because the median is terrible is not a working service |
| submission call (`POST /v1/claims`) p99 | ≤ 500 ms | the front door must stay responsive while the back end is busy; this is what makes the asynchronous design worth having |
| error ratio (`failed` + `timeout`) | ≤ 1 % | escalations are deliberately **not** counted here — see below |

## Why 45 seconds, from section 1 of the brief

The brief describes the pipeline as "high-volume, latency-sensitive,
correctness-critical". Those three pull against each other, and 45 seconds is
where we think the trade sits for *this* stage of *this* pipeline.

**It is not a customer-facing wait.** This is not the FNOL form submitting. The
FNOL submission returns in milliseconds — that is the `POST /v1/claims`
objective above. What takes 45 seconds is the triage recommendation that lands
in a claims handler's work queue. The customer is already off the call.

**It has to be faster than the handler needs it.** A handler picks up a claim
from a queue and expects the triage recommendation to be there. In an Indian
motor claims operation, first touch on a non-urgent claim is measured in hours;
the insurer's own claims procedure chapter commits to appointing a surveyor
"within 24 hours of reporting". A recommendation that arrives inside a minute is
comfortably inside that, with room for the queue to back up during a morning
peak without a handler ever waiting on us.

**It cannot be much tighter, because the work is irreducibly several model
calls.** A run makes five model calls on the happy path — classify, plan
modifiers, analyse, verify, route — and up to eight when the verifier sends the
work back. At a realistic 2–4 s per call on a free tier, the floor is roughly
10–30 s before any of our own work. Declaring 20 s would be declaring a target
that depends entirely on the provider's latency on the day, which is exactly
what D6 tells us to stop measuring.

**Why not a generous 120 s.** Because then the queue is the real limit and
nobody notices it: at 120 s per run a single worker sustains 0.5 runs/minute,
and a 200-claim morning takes seven hours. 45 s forces the throughput question
to be answered rather than hidden, which is the point of the scaling work in
[scaling.md](scaling.md).

## What the objective deliberately excludes

**Escalations are successes, not errors.** A run that declines to answer and
routes to a human has done the right thing (F7) and is counted in
`triage_runs_finished_total{status="escalated"}`, tracked on its own panel. If
escalations counted against the error budget, the cheapest way to meet the SLO
would be to answer everything confidently, which is the single worst outcome
this system can produce. There is a separate alert, `EscalationRateHigh`, for
when *too many* runs escalate — that is a retrieval or verifier problem, not an
availability one.

**Degraded runs count toward the latency objective.** A run that lost the
provider and fell back to retrieval-only evidence still has to come back
quickly; a degraded path that hangs is worse than one that fails.

**Rate-limited submissions are excluded.** A 429 is the system working. The
Locust task reports them separately rather than as fast successes, which would
flatter p95.

## How it is measured

The histogram is observed **once per run, in the worker**, spanning
`queued_at` → `completed_at`. That is deliberate: measuring the submission call
alone would report about 4 ms and be meaningless, and measuring inside the graph
would exclude queue wait, which is the first thing to degrade under load.

The load test replaces the model provider with the `llm-stub` service, reached
through the ordinary OpenAI-compatible adapter (`LLM_BASE_URL=http://llm-stub:9009/v1`).
The stub answers after a configurable, realistic delay. This is required by D6
and it is also the only way to get a repeatable number: a free tier's rate
limiter would otherwise be the thing under test.

Because the stub's latency is a parameter, the measured p95 is only meaningful
alongside it. Every result in [load-test.md](load-test.md) records the stub
latency it was taken at.

## Dashboard and alerting

* Panel: *p95 run latency (submission → final result)*, thresholded green/amber/red
  at 35 s and 45 s.
* Recording rule: `triage:run_duration_seconds:p95`. The dashboard and the SLO
  report both read this one expression, so they cannot disagree.
* Alert: `SLOBreach` fires when p95 > 45 s for 5 minutes, and points at the
  runbook entry for a queue backlog.
