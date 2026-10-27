# Requirements traceability

All 22 numbered requirements plus the eight functional ones, with where each is
implemented and what state it is actually in. The brief says completing all 22
is not expected and that sound prioritisation is itself assessed, so the status
column is honest rather than generous.

**Status key**

| | |
|---|---|
| **done** | implemented and verified by tests or by a measurement in this repository |
| **done, unverified** | implemented and reviewed, but the thing that would prove it has not been run (almost always: it needs Docker, or a real model) |
| **partial** | the mechanism exists; something material about it is missing, and what is named |
| **not done** | not built |

---

## Section 4 — What the system must do

| # | requirement | status | where |
|---|---|---|---|
| **F1** | Accept an FNOL over a documented REST interface, against a published data contract | **done** | `claimtriage/api/app.py`, `contracts/`, `GET /contracts/{name}` serves live JSON Schema generated from the enforcing models. [data-contracts.md](data-contracts.md) |
| **F2** | Schema, range, categorical and cross-field validation at the ingestion boundary. Quarantine rejects | **done** | `claimtriage/validation.py` — 4 layers, 14 cross-field rules. 29 tests. `agent_run_started` typed `Literal[False]` |
| **F3** | Classify the claim and extract the entities downstream stages need | **done** | `intake_classification` node; `Classification` in the contract. Classification *quality* needs a real model |
| **F4** | Retrieve the relevant clauses, including any restriction or exclusion that limits them | **done** | hybrid retrieval + role filter + cross-reference navigation. [retrieval.md](retrieval.md), [difficult-case.md](difficult-case.md) |
| **F5** | A coverage position grounded in retrieved clauses, with clause-level citations | **done** | `coverage_analysis` node; `citations[]` with `clause_id`, `section_path`, `page`, verbatim `quote`, and `resolves` guaranteed true |
| **F6** | Route to fast-track, surveyor, or investigation, with a reason | **done** | `routing` node + the deterministic floor in `agents/signals.py` |
| **F7** | Escalate instead of answering when retrieval is weak, clauses conflict, or the claim is outside the corpus | **done** | 8 escalation reasons; `weak_retrieval` below `ESCALATION_MIN_RETRIEVAL_SCORE`, `conflicting_clauses` from the verifier, `verification_failed`, `loop_budget_exhausted`. Honest *recognition* of an out-of-corpus question needs a real model — the stub always answers |
| **F8** | Expose health, metrics and structured logs for the whole pipeline | **done** | `/health`, `/health/ready`, `/health/corpus`, `/metrics` on the API and on the worker's admin port; structlog JSON throughout |

---

## Section 6 — Agent system requirements

| # | requirement | status | where |
|---|---|---|---|
| **A1** | LangGraph or CrewAI; retrieval a tool the agent chooses; ≥3 tools; bound the loop by steps, tool calls and wall clock | **done** | LangGraph. **Five** tools (`agents/tools.py`). `plan_modifiers` may answer "no modifiers" and skip retrieval entirely — `_after_plan`. Three independent bounds, all configurable |
| **A2** | ≥2 agents with distinct responsibilities and a defined hand-off; document the topology and mid-run failure | **done** | **Four** agents. [agent-topology.md](agent-topology.md) has the topology, the ownership table and an 11-row mid-run failure table, including the known at-most-once queue gap |
| **A3** | Verify every answer against the retrieved passages before returning it; every citation must resolve; retry with a different strategy or escalate | **done** | Two-stage verifier: deterministic quote resolution, then model entailment. Only resolving citations reach the result. Three-rung strategy ladder. `test_every_returned_citation_resolves`, `test_completed_runs_are_always_verified` |
| **A4** | FastAPI; asynchronous with a run id and polling or callback; justify; a queue between submission and execution | **done** | 202 + `run_id`, poll `/v1/runs/{id}`, optional `X-Callback-Url`. Redis list between API and worker. Justification in [architecture.md](architecture.md#why-asynchronous-specifically) |
| **A5** | Timeouts everywhere, bounded retry with backoff, a circuit breaker, graceful degradation along a defined fallback path; all demonstrable; an incomplete run must not be returned as a confident answer | **done** | `llm/base.py`. Exponential backoff with full jitter. Three-state breaker. 11 defined fallback paths. **Exercised end to end** — observed output in [RUNBOOK.md](../RUNBOOK.md#verifying-the-whole-path-before-a-demonstration) |
| **A6** | Per agent, on the dashboard: task success rate, steps and tool calls per run, end-to-end and per-step latency, retry rate, container uptime | **done, unverified** | All six metrics exported and all six have dashboard panels (`ops/grafana/dashboards/claim-triage.json`). The metrics are verified live; the **dashboard rendering is not**, because Grafana has not been started |
| **A7** | `docker compose up --scale <worker>=3` must work; measure the effect on throughput and p95; if scaling does not help, identify the bottleneck | **partial** | The topology supports it — stateless worker, no bound host port, Prometheus `dns_sd_configs` for the replicas. **Not measured.** [scaling.md](scaling.md) states the capacity model, the method, the predictions and a decision procedure for each candidate bottleneck, rather than a fabricated result |
| **A8** | A simple web client that submits, obtains the result, and displays the recommendation with citations and the escalation path | **done, unverified** | `web/index.html` — seven preset scenarios, polling, citations each resolvable to the full clause, the step trace, guardrail events, the escalation path, feedback buttons. Served by nginx or by the API. **Not opened in a browser** |

---

## Section 7 — LLMOps requirements

| # | requirement | status | where |
|---|---|---|---|
| **L1** | LangChain or LlamaIndex for retrieval; FAISS or Chroma; document the chunking and embedding strategy and how it addresses the corpus difficulty; re-embedding without rebuilding the whole index | **partial** | Chroma (plus a local numpy store for dev). Three-pass structure-aware chunker, role tagging, cross-reference extraction, hybrid dense+BM25 with RRF — all documented in [retrieval.md](retrieval.md) and tested. Per-document re-indexing keyed on SHA-256, tested. **The deviation:** retrieval is orchestrated by our own code rather than by LangChain or LlamaIndex. `langchain-core` is a dependency via LangGraph but the retrieval chain is hand-written — see the note below |
| **L2** | Prompts as versioned artifacts; record the version with every response; compare ≥2 variants on the same evaluation set and report which won, on what, by how much | **partial** | Registry with content hashes; `prompt_versions` on every result and every step record; two complete sets with the four changes and their rationale in `prompts/registry.yaml`; `prompt-compare` runs both over the same set and diffs them. **The comparison has not been run against a real model** — the stub cannot read prompt text, so it returns a zero delta, and `prompt-compare` now refuses rather than printing zeros. [evaluation.md](evaluation.md#l2-comparing-two-prompt-sets) |
| **L3** | ≥20 scenarios with expected outcome and supporting passage, including the 4.1 case, runnable from the CLI; TruLens or equivalent; report groundedness, context relevance, faithfulness and hallucination rate, stating how each is computed; then one deliberate change, all four before and after | **partial** | **25 scenarios**, CLI-runnable, each with its supporting passage. Four metrics with stated computation and thresholds. Deliberate change (`RETRIEVAL_TOP_K` 6→12) measured before and after with a direction-aware diff. **All numbers were taken against the stub** and are marked `valid_for_reporting: false`. TruLens not used — equivalent implemented, [reasons here](evaluation.md#why-not-trulens) |
| **L4** | Prompt injection defence on every free-text field reaching a model; output filtering so the system cannot issue an unauthorised determination; toxicity detection on input and output | **done** | Three surfaces — input, tool, output — in `claimtriage/guardrails.py`, 35 tests. **The deviation:** rules, not a classifier. Deliberate, for explainability and cost; the limitation is stated in the module docstring and in the report |
| **L5** | Prompt tokens, completion tokens and cost at the time of the call, attributed per step and per run; cost per 100 runs and the largest single cost driver | **done** | `CostBreakdown` with `per_step`, `tokens_per_step`, `largest_driver` and `largest_driver_basis`. **Measured:** 15,390 tokens/run, 1.54 M per 100 runs, largest driver `coverage_analysis` at 32%. [cost.md](cost.md) |
| **L6** | A feedback endpoint with a reason, persisted and aggregated; automatically score a sample of live runs against the evaluation metrics, charting quality over time | **done** | `POST /v1/feedback`, `GET /v1/feedback/summary` with by-queue aggregation and a `corrections` list. 20% of completed runs scored automatically in the worker, exposed as `triage_live_quality_score{metric}` and charted |

### Note on L1 and the orchestration framework

L1 asks for retrieval to be orchestrated with LangChain or LlamaIndex. It is not.
Retrieval here is `claimtriage/retrieval/` — about 600 lines of our own code over
Chroma, with a hand-written hybrid retriever and RRF fusion.

That was a judgement call and it should be visible rather than buried. The
chunking this corpus needs is the three-pass, heading-aware, cross-reference-
extracting pass described in [retrieval.md](retrieval.md); none of the
off-the-shelf splitters produce it, so the chunker would have been written by
hand regardless. Having written it, wrapping it in a `LangChain` `Retriever`
interface to satisfy the letter of the requirement would have added an
abstraction layer and a dependency without changing a line of the logic. The
honest description is "the requirement is met in substance and not in letter",
which is why it is marked partial rather than done.

LangGraph *is* used, as A1 requires, and `langchain-core` comes with it.

---

## Section 8 — Platform and deployment requirements

| # | requirement | status | where |
|---|---|---|---|
| **D1** | A data contract per external interface; schema, type, range, category and cross-field validation before any agent starts; quarantine rejects with field-level errors; a malformed request must not start an agent run | **done** | Four contracts. Validation in the API process before a run id is issued. `QuarantineRecord` with field-level errors, retrievable and listed. `agent_run_started: Literal[False]` |
| **D2** | An idempotent pipeline — read, parse, chunk, embed, index — invocable from the CLI; record data freshness (when, from which versions, how many chunks) and throughput, and expose both as metrics | **done** | `cli ingest`, keyed on SHA-256. Freshness to Redis *and* `index_meta.json`; `GET /health/corpus`; five gauges. Measured: 381 chunks from 2 documents in 5.1 s, 74.6 chunks/s |
| **D3** | ≥4 services under Compose, from a single `docker compose up` on a clean clone, with dependencies and health checks declared; nothing outside containers; justify the deployment boundaries | **done, unverified** | **Ten** services, every one with a health check and declared `depends_on` conditions. Boundaries justified in [architecture.md](architecture.md#where-the-deployment-boundaries-are-and-why), including four considered and rejected. **`docker compose up` has not been run** |
| **D4** | No credentials, endpoints or model identifiers anywhere in the repository; all environment-specific config injected at runtime, with a documented `.env.example` | **done** | `.env.example` documents 50+ variables with the reasoning. No default in `config.py` points at a real endpoint or model. `.env` is git-ignored. **One exception, named rather than hidden:** `gemini_provider.DEFAULT_BASE` holds Google's single public API base, overridable by `LLM_BASE_URL` — the reasoning is in a comment at the line, and the repository is scanned for credentials as part of the final check |
| **D5** | Structured logging, a correlation ID across every service and agent step, OpenTelemetry to Jaeger or Tempo, Prometheus scraping every service, a Grafana dashboard with p50/p95, error rate, throughput, availability and per-container resource utilisation; given a run id, the entire run reconstructible | **partial** | Structlog JSON with correlation ID, run ID and step on every line. OTel over OTLP/HTTP to Jaeger. Prometheus scrapes api, worker (via DNS SD, so all replicas), stub and cAdvisor. Dashboard has every required panel. `GET /v1/runs/{id}/trace` reconstructs a run completely — **verified**. **Unverified:** the Jaeger export (tests run with tracing off) and the dashboard rendering |
| **D6** | Declare a p95 SLO before measuring, justified against section 1; measure a complete run, not the submission call; Locust with the LLM stubbed; report RPS, p50, p95 against the target and whether it was met; identify the concurrency at which it fails and the component responsible | **partial** | SLO declared first, in a separate file, written into `.env.example`, the Prometheus alert and the dashboard threshold. Locust measures the complete run. **Measured:** 15 runs/s ceiling, p95 2.6 s at 50 callers, target fails at ~640 callers, bottleneck identified as the 40-thread pool with `triage_inflight_runs` as evidence. **The deviation:** measured outside Docker in `EXECUTION_MODE=inline`, so the ceiling and the bottleneck are artefacts of that topology. [load-test.md](load-test.md) is explicit about which numbers transfer |
| **D7** | Caching — of embeddings, retrievals or whole runs, justified — and per-client rate limiting; report the measured effect of caching on p95 and cost per run | **done** | Three cache layers, each with its key and justification. Fixed-window per-client rate limiter, verified engaging at request 31 of 30/60s. **Measured:** run cache cuts p95 95% and tokens 95% on repeated traffic, nothing on unique; embedding and retrieval caches ~5% of tokens and no latency. The unflattering result is the reported one |
| **D8** | A runbook for the two most likely failure modes — symptom, confirmation, recovery — and an incident log with at least one genuine entry from development | **done** | [RUNBOOK.md](../RUNBOOK.md): two failure modes in depth with a confirmation decision table each, plus eight shorter entries. [INCIDENT_LOG.md](../INCIDENT_LOG.md): **six** genuine incidents, each with cause, cost and the guard added |

---

## Section 9 — Demonstration items

| # | item | state |
|---|---|---|
| 1 | `docker compose up` from a clean clone to a working system | **unverified** — the one thing to check first |
| 2 | A valid request producing a routed recommendation with citations that resolve | **verified** — `scripts/demo.sh`, and smoke check 2 |
| 3 | One run traced end to end: every tool call, query, passage, prompt version, per-step latency and cost | **verified** — `/v1/runs/{id}/trace`, smoke check 3 |
| 4 | A malformed request rejected and quarantined, with evidence no agent run started | **verified** — smoke check 4, plus `test_rejection_never_reports_a_started_agent_run` |
| 5 | A case the corpus cannot answer producing an escalation rather than a fabrication | **partial** — the path works and is tested; honest recognition needs a real model, so this fails against the stub |
| 6 | Self-verification catching an unsupported answer, followed by a further search or escalation | **verified** — observed in an early run: 3 verification failures, 2 strategy changes, then `loop_budget_exhausted`. `test_retry_uses_a_different_strategy_each_time` |
| 7 | A prompt injection attempt and the guardrail response | **verified** — smoke check 7, quarantined at `guardrail` with three patterns named, no run started |
| 8 | The circuit breaker opening when the provider is unavailable, and the degradation path | **verified** — exercised against the stub service. `triage_circuit_state` 0→2→0, fallback chain visible in `degradation_path` |
| 9 | Locust results against the declared target, the concurrency at which it fails, and the effect of `--scale` | **partial** — Locust results and the knee measured; `--scale` not measured |
| 10 | The section 4.1 case handled correctly | **verified against the stub** — smoke check 10 passes position, queue and the citation assertion. The *reasoning* needs a real model |

---

## Prioritisation

The brief asks for a working end-to-end path by week two, with requirements
added after. The order things were actually built, and why:

1. **Contracts and the ingestion boundary** first, because D1's "a malformed
   request must not start an agent run" is a structural property. Retrofitting it
   means auditing every path into the agent; building it first means there is
   only one.
2. **The corpus pipeline**, because everything downstream is meaningless without
   a correctly attributed index — and this is where the worst bug was (INC-001).
3. **The agent graph with verification built in**, not added later. A3 is the
   hardest requirement to retrofit: a verifier bolted onto a finished pipeline
   can only reject, where one in the graph can send work back.
4. **Observability alongside**, because the alternative is debugging an agent
   loop by reading logs, which does not work.
5. **Evaluation, resilience, cost, caching, load testing** — in that order,
   because each one needs the previous to be measurable.

What was consciously **not** done, and why:

- **`--scale worker=3` measurement.** Needs Docker. Predicting it honestly
  ([scaling.md](scaling.md)) is worth more than a number that cannot be taken.
- **A model-based guardrail.** Would add a model call per field on a free tier
  with a low rate limit, and make a block unauditable.
- **LangChain's retrieval chain.** The chunker had to be hand-written for this
  corpus; wrapping it changes no logic.
- **Trimming the verifier's context to cited passages.** A known 25% token
  saving, deliberately left undone and recorded, because it touches the
  verification path and was not worth the risk late on.
- **At-least-once queue delivery.** Documented as a known gap with the fix
  named. The claim is never lost, only the job, and a stuck run is detectable.
