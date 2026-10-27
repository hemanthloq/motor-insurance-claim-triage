# Motor Insurance Claim Intake and Triage

An advisory multi-agent service for a motor insurer's First Notice of Loss
(FNOL) pipeline. It accepts an FNOL over a published data contract, validates it
at the ingestion boundary, works out what kind of claim it is, determines the
likely coverage position from the insurer's own published policy wording with
clause-level citations, verifies that answer against the passages it actually
retrieved, and routes the claim to fast-track, a surveyor, or investigation with
a stated reason.

**It never approves, rejects or values a claim.** It produces a recommendation
with supporting evidence. A human claims handler decides. If it cannot support
an answer from the corpus, it escalates instead of answering — that behaviour is
the point of the system, not a limitation of it.

- Course: UE24AM342AA5 — ML System Design and AgentOps · Domain: General Insurance
- Corpus: ICICI Lombard **Private Car Package Policy** (product 3001, UIN IRDAN115RP0017V01200102)

---

## Contents

- [Quick start](#quick-start)
- [What you should see](#what-you-should-see)
- [The hard case this is built around](#the-hard-case-this-is-built-around)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Everyday commands](#everyday-commands)
- [Running without Docker](#running-without-docker)
- [Evaluation](#evaluation)
- [Load testing](#load-testing)
- [Observability](#observability)
- [Repository layout](#repository-layout)
- [Documentation](#documentation)
- [Troubleshooting](#troubleshooting)
- [What is not finished](#what-is-not-finished)

---

## Quick start

Two ways in. **A** needs only Python and takes about two minutes. **B** is the
real deployment. Neither needs `make` — every target in the `Makefile` is a
convenience wrapper over the commands shown here, so if you do not have `make`
(most Windows installs do not), just run the commands.

---

### A — one process, no Docker, no API key

Everything in one Python process: the in-memory state backend, a local vector
store on disk, and the in-repo stub provider. No Redis, no Chroma, no key.

```bash
git clone https://github.com/hemanthloq/motor-insurance-claim-triage
cd motor-insurance-claim-triage

python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Windows
# .venv/bin/python -m pip install -r requirements-dev.txt         # Linux / macOS

bash scripts/run-local.sh
```

That script downloads the insurer's two published documents, builds the clause
index, and serves on <http://localhost:8080>. First run takes a minute or two
for the download and the index; after that it starts in a couple of seconds.

Then, in a second terminal:

```bash
bash scripts/demo.sh    http://localhost:8080   # the section 4.1 case, end to end
bash scripts/smoke.sh   http://localhost:8080   # PASS/FAIL for all ten demo items
.venv/Scripts/python.exe -m pytest              # 167 tests, ~4 seconds
```

**What this path does and does not show.** Every endpoint, contract, validation
rule, guardrail, agent hand-off, verification step and trace is exercised for
real. What is *not* real is the model: the stub cannot read a policy clause, so
the coverage *reasoning* is placeholder text and three of the 25 evaluation
scenarios fail by design. Add a key (below) and the same command uses it.
See [docs/stub-provider.md](docs/stub-provider.md).

To use a real model on this path, export three variables first:

```bash
export LLM_PROVIDER=openai_compat
export LLM_BASE_URL=https://api.groq.com/openai/v1
export LLM_API_KEY=gsk_...
export LLM_MODEL=llama-3.3-70b-versatile
bash scripts/run-local.sh
```

Useful flags: `--reindex` to rebuild the index, `--port 9000` to move it.

---

### B — the full system under Docker

**You need:** Docker with Compose v2, about 6 GB of free disk, and an API key for
any model provider with a free tier. Nothing else — no Python on the host.

```bash
# 1. configure. Nothing secret is committed; the blanks in .env.example are blank
#    on purpose and the file documents every one of them.
cp .env.example .env
$EDITOR .env          # fill in LLM_BASE_URL, LLM_API_KEY, LLM_MODEL

# 2. start everything
docker compose up --build -d

# 3. fetch the insurer's published documents and build the index.
#    This is a separate step because the corpus is not committed to the
#    repository -- see corpus/sources.yaml for what gets downloaded and from where.
docker compose run --rm tools corpus-fetch
docker compose run --rm tools ingest

# 4. check it is ready
curl -s localhost:8080/health/ready  | python -m json.tool
curl -s localhost:8080/health/corpus | python -m json.tool
```

Then open **<http://localhost:3000>** and press *Submit*. The form is
pre-loaded with the section 4.1 reference case; the buttons along the top switch
between the other scenarios.

> This path has **not been run by the author** — Docker is not installed on the
> machine this was built on. Every service, Dockerfile stage and health check is
> written and reviewed, and path A is verified end to end, but the container
> build itself is unexercised. If something here fails, that is why; see
> [What is not finished](#what-is-not-finished).

### Filling in `.env`

Any OpenAI-compatible endpoint works. Two that have a usable free tier:

```bash
# Groq
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=gsk_...
LLM_MODEL=llama-3.3-70b-versatile

# Google Gemini
LLM_PROVIDER=gemini
LLM_API_KEY=AIza...
LLM_MODEL=gemini-2.0-flash
```

You can also run with **no provider at all**, which is useful for checking the
plumbing before you have a key:

```bash
LLM_PROVIDER=openai_compat
LLM_BASE_URL=http://llm-stub:9009/v1
LLM_MODEL=stub-1
LLM_API_KEY=not-needed
```

The `llm-stub` service answers like a model would, after a realistic delay,
without calling anything. Every endpoint, every guardrail and every trace works.
What it cannot do is read a policy clause, so the coverage reasoning will be
placeholder text and **no evaluation number taken against it means anything** —
the evaluation harness refuses to write a report unless you override it
explicitly. See [docs/stub-provider.md](docs/stub-provider.md).

### Where everything is

| | URL | notes |
|---|---|---|
| Web client | <http://localhost:3000> | submit a claim, see the citations |
| API docs | <http://localhost:8080/docs> | OpenAPI, generated from the contracts |
| Grafana | <http://localhost:3001> | anonymous viewer; dashboard *Claim Triage* |
| Prometheus | <http://localhost:9090> | |
| Jaeger | <http://localhost:16686> | pick service `claim-triage-worker` |
| Locust | <http://localhost:8089> | `docker compose --profile loadtest up -d locust` |

---

## What you should see

A valid claim, submitted from the command line:

```bash
scripts/demo.sh
```

```
submitting the section 4.1 reference case to http://localhost:8080
  run run-ca9a8fbc3a5b47a0
  status: completed

==============================================================================
  status      completed
  position    likely_not_covered
  queue       investigation  (priority high, first touch 8h)
  verified    verified  support 0.93  citations 3/3
==============================================================================

  The vehicle appears to have been in use for a purpose the policy does not
  permit at the time of the loss.

  a handler must establish:
    - Was the delivery carried out for reward or as a personal favour?

  citations (3):
    - ICICI Lombard - Private Car Package Policy - Policy Wording
      CHAPTER 4 - Limitations as to Use  (page 19, restriction)
      "The Policy covers use of the vehicle for any purpose other than: Hire or
       Reward, Carriage of goods (other than samples of personal luggage) ..."
      il_private_car_package_wording::chapter-4-limitations-as-to-use::0151
    ...

  steps 7  tool calls 6  tokens 15390  cost USD 0.0  12.4s
  largest cost driver: coverage_analysis
```

To check every demonstration path at once, including the rejection,
escalation, injection and circuit-breaker paths:

```bash
scripts/smoke.sh
```

It prints PASS or FAIL for each of the ten demonstration items. Run it once
before any demonstration so nothing is discovered live.

---

## The hard case this is built around

> *"I was making a food delivery run in my own car when the accident happened."*

Search for that and you get the own-damage cover, which grants the claim. The
provision that actually governs the outcome is in a different chapter, is about
the purposes a vehicle may be used for, and shares almost no vocabulary with
what the customer said:

> **Chapter 4 — Limitations as to Use.** The Policy covers use of the vehicle for
> any purpose other than: Hire or Reward, **Carriage of goods** (other than
> samples of personal luggage), Organised racing, Pace Making, Reliability
> trails or Speed testing, any purpose in Connection with Motor Trade.

and it is connected to the grant by a third clause in a second place:

> **Chapter 2, General Exceptions III(a).** The Company shall not be liable …
> whilst the vehicle insured herein is being used otherwise than in accordance
> with the **'Limitations as to Use'**.

A single embedding search over the customer's words will not return either of
them. Three things in this system are there specifically because of that, and
they are the design, not decoration:

1. **The chunker records cross-references.** It notices that the general
   exception *names* the Limitations as to Use, and stores that link at index
   time. The agent can then follow the reference by name rather than hope a
   vector search lands on it.
2. **Chunks are tagged by role** — grant, restriction, exclusion, condition,
   procedure, definition — so the agent can ask "what, anywhere in this wording,
   could take the cover away?", a question no phrasing of the claim will answer.
3. **A planning step decides whether to look.** After finding the grant, the
   Coverage Analyst is asked explicitly whether any of four things could displace
   it, and what to search for in the policy's own vocabulary. It is allowed to
   answer "nothing applies" and stop.

[docs/difficult-case.md](docs/difficult-case.md) sets this out in full, with the
exact clauses, the test values, and the reference answer.

---

## Architecture

```
                      ┌──────────────┐
   browser ──────────▶│  web (nginx) │
                      └──────┬───────┘
                             │ /v1, /health, /contracts proxied
                      ┌──────▼────────────────────────────────────┐
                      │  api  (FastAPI)                           │
   POST /v1/claims    │   • contract validation                   │
   ──────────────────▶│   • cross-field rules                     │──┐
   202 + run_id       │   • guardrails on free text               │  │ quarantine
                      │   • rate limit per client                 │  │ (no run starts)
                      │   • enqueue. NOTHING ELSE.                │◀─┘
                      └──────┬────────────────────────────┬───────┘
                             │ Redis list                 │ run store
                      ┌──────▼───────┐             ┌──────▼───────┐
                      │    redis     │◀───────────▶│              │
                      └──────┬───────┘             └──────────────┘
                             │  (scale: worker=3 compete on one list)
              ┌──────────────▼──────────────────────────────────┐
              │  worker  (LangGraph)                            │
              │                                                 │
              │  intake_analyst ──▶ coverage_analyst ──▶ verifier│
              │                          │  ▲            │      │
              │                     tools│  └── retry ───┘      │
              │                          ▼     (new strategy)   │
              │                   ┌──────────┐          ▼       │
              │                   │  chroma  │   routing_officer│
              │                   └──────────┘                  │
              └───────┬─────────────────────────────────┬────────┘
                      │ OTLP                            │ /metrics :9100
              ┌───────▼──────┐   ┌────────────┐   ┌─────▼──────┐
              │    jaeger    │   │  llm-stub  │   │ prometheus │──▶ grafana
              └──────────────┘   └────────────┘   └────────────┘
                                                   ▲ cadvisor
```

Ten services. Where the deployment boundaries are and why each one is a boundary
is argued in [docs/architecture.md](docs/architecture.md) and restated at the top
of `docker-compose.yml`; the short version is that `api` and `worker` are split
on scaling and failure isolation, `worker` and `chroma` on resource profile and
release cadence, and the observability stack is separate so that it outlives the
thing it is observing.

The four agents, their hand-offs, the loop bounds and what happens when one
fails mid-run are in [docs/agent-topology.md](docs/agent-topology.md).

---

## Configuration

Every environment-specific value is injected at runtime. There is no credential,
endpoint or model identifier anywhere in the source tree — `.env.example`
documents all of them with the reasoning, and `docker-compose.yml` passes them
through with defaults for everything that is not a secret.

The settings most worth knowing about:

| variable | default | what it does |
|---|---|---|
| `LLM_PROVIDER` | `openai_compat` | `openai_compat`, `gemini` or `stub` |
| `PROMPT_SET` | `v2` | which versioned prompt set to run (`v1` is the baseline) |
| `AGENT_MAX_STEPS` / `AGENT_MAX_TOOL_CALLS` / `AGENT_WALL_CLOCK_SECONDS` | 14 / 12 / 120 | the three loop bounds |
| `VERIFIER_MIN_SUPPORT` | 0.60 | below this the answer is not returned |
| `EMBEDDING_BACKEND` | `sentence_transformers` | `hash` is an offline fallback; see below |
| `CIRCUIT_FAIL_THRESHOLD` / `CIRCUIT_RESET_SECONDS` | 5 / 30 | provider circuit breaker |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | 30 / 60 | per client; this is a budget control |
| `SLO_P95_SECONDS` | 45 | the declared objective, read by the dashboard and the load test |

**Embeddings run locally and cost nothing.** `sentence-transformers`
`all-MiniLM-L6-v2`, 384 dimensions, about 90 MB, downloaded once into the
`index-data` volume on first use. If it cannot be downloaded — an air-gapped
machine, a first-run failure — the system falls back to `hash`, a deterministic
character-trigram projection. That keeps the service up and keeps lexical search
working, but it is **not** semantic search, and `/health/ready` says which
backend is live so it can never be mistaken for working:

```json
{ "corpus_index": { "ok": true, "chunks": 381, "embeddings": "hash" } }
```

---

## Everyday commands

`make help` lists everything. **`make` is optional** — each target is a thin
wrapper, and the command it runs is printed in the `Makefile` if you would
rather run it directly. The ones you will use:

```bash
make up                 # build and start the system
make corpus && make ingest   # download the documents and build the index
make ps                 # service health
make logs               # follow api + worker
make demo               # submit the section 4.1 case and print the result
make smoke              # check all ten demonstration paths
make trace RUN=run-...  # the full trace for one run
make scale              # three agent workers
make eval               # run the 25-scenario evaluation set
make prompt-compare     # run both prompt sets and diff them
make loadtest-headless  # 3 minutes at 20 callers, CSV to loadtest/reports
make circuit-open       # trip the provider circuit breaker
make outage / make recover   # simulate a provider outage
make down               # stop, keep the volumes
make nuke               # stop and delete the volumes (index and runs are lost)
```

Without `make`, and without Docker, the equivalents are
`bash scripts/run-local.sh`, `bash scripts/demo.sh`, `bash scripts/smoke.sh`
and `.venv/Scripts/python.exe -m pytest`.

---

## Running without Docker

This is [path A](#a--one-process-no-docker-no-api-key) above:
`bash scripts/run-local.sh`. `scripts/dev.env` holds the profile it loads.

If you prefer to drive it by hand, or want the pieces separately, the launcher
is doing exactly this:

```bash
set -a; . scripts/dev.env; set +a          # load the no-Docker profile
export PYTHONPATH=.

.venv/Scripts/python.exe -m claimtriage.cli ingest        # build the index
.venv/Scripts/python.exe -m uvicorn claimtriage.api.app:app --port 8080
```

Other things worth knowing on this path:

```bash
# the CLI, without the service running at all
.venv/Scripts/python.exe -m claimtriage.cli --help
.venv/Scripts/python.exe -m claimtriage.cli search "hire or reward" --strategy lexical
.venv/Scripts/python.exe -m claimtriage.cli triage my-claim.json
.venv/Scripts/python.exe -m claimtriage.cli index-info

# the API and the worker as separate processes, against a local Redis
EXECUTION_MODE=queue REDIS_URL=redis://localhost:6379/0 \
  .venv/Scripts/python.exe -m uvicorn claimtriage.api.app:app --port 8080
EXECUTION_MODE=queue REDIS_URL=redis://localhost:6379/0 \
  .venv/Scripts/python.exe -m claimtriage.worker.executor

# the stub provider as a service -- needed for demonstration item 8
.venv/Scripts/python.exe -m uvicorn claimtriage.stub.app:app --port 9009
```

On Linux or macOS substitute `.venv/bin/python` throughout.

---

## Evaluation

A fixed set of **25 scenarios** in `eval/dataset.yaml`, each with its expected
outcome and the passage in the corpus that supports it. It covers the section 4.1
reference case and a deliberate control for it (commuting to work is also a
"purpose", and must *not* be treated as restricted), the restricted-use family,
the driver's clause, conditions, two out-of-corpus questions that must escalate,
two guardrail cases and four contract-validation cases that must never start a
run.

```bash
make eval                      # writes eval/reports/latest.json
make eval-judge                # adds an LLM-as-judge scoring pass
docker compose run --rm tools eval --only S01_food_delivery_reference_case
```

Four metrics are reported, and [docs/evaluation.md](docs/evaluation.md) states
exactly how each is computed, what the thresholds are and why they are where
they are:

- **groundedness** — share of the determination's statements supported by a retrieved passage
- **context relevance** — share of the passages shown to the model that bear on the claim
- **faithfulness** — share of citations whose quote is found character-for-character in the clause it names
- **hallucination rate** — share of records that asserted a confident position they could not support. *Escalating is not counted as a hallucination* — declining to answer is the required behaviour and must not be punished by the metric meant to reward honesty.

The same file carries the prompt-set comparison (L2) and the before/after of one
deliberate change (L3).

---

## Load testing

The objective was **declared before anything was measured** —
[docs/slo.md](docs/slo.md), `SLO_P95_SECONDS=45`, also written into the
Prometheus alert and the Grafana threshold. The measurements are in a separate
file, [docs/load-test.md](docs/load-test.md), added afterwards.

```bash
make loadtest-headless     # or: make loadtest, for the Locust UI on :8089
```

The load test measures **a complete run, submission to final result**, not the
submission call — the submission call returns in about 4 ms and measuring it
would be meaningless. The model is replaced by the `llm-stub` service, reached
through the ordinary OpenAI-compatible adapter, so the test measures our system
rather than a free tier's rate limiter.

---

## Observability

Given a run identifier, the entire run is reconstructible:

```bash
curl -s localhost:8080/v1/runs/<run-id>/trace | python -m json.tool
```

That returns every tool call with the query it issued and the clauses it
returned, every prompt version with its content hash, per-step latency and token
cost, the verification outcome with the citation resolution rate, the index
version the answer was grounded in, the guardrail events, and the degradation
path if one was taken.

One correlation ID follows a claim from the HTTP request, through the queue, into
the worker, down every agent step and out into the logs, the metrics and the
trace. Supply your own with `X-Correlation-ID` and it is used and echoed back.

```bash
# every log line for one run, across all containers
docker compose logs --no-log-prefix | grep '"run_id":"run-abc123"'
```

The Grafana dashboard carries p50/p95 latency against the objective, error rate,
throughput, availability, per-agent task success rate, steps and tool calls per
run, per-step latency, retry rate, container uptime, circuit-breaker state,
tokens and spend by step, cost per 100 runs, cache hit ratio, index freshness and
per-container CPU and memory.

---

## Repository layout

```
claimtriage/
  contracts/        the published data contracts -- single source of truth
  validation.py     the ingestion boundary: schema, range, category, cross-field
  guardrails.py     injection, toxicity, unauthorised-output filtering
  retrieval/        loaders, structure-aware chunker, embeddings, index, ingest
  llm/              provider interface, adapters, retry, circuit breaker, stub
  prompts/          the versioned-prompt registry
  agents/           LangGraph topology, the five tools, routing signals, runner
  eval/             the metrics and the evaluation harness
  api/              FastAPI
  worker/           queue consumer and the worker admin port
  stub/             the llm-stub service
  cli.py            ingest, eval, prompt-compare, triage, export-contracts
prompts/v1, v2      the prompt artifacts, with registry.yaml
corpus/sources.yaml what gets downloaded, from where, and why those documents
eval/dataset.yaml   the 25 scenarios
ops/                prometheus, grafana, nginx
loadtest/           the Locust file
scripts/            demo.sh, smoke.sh, dev.env
docs/               see below
tests/              167 tests
```

---

## Documentation

| | |
|---|---|
| [REPORT.md](REPORT.md) | the project report: scope, design, what was rejected, results, what did not work |
| [docs/presentation/](docs/presentation/) | the 15-slide presentation deck, its generator and its QA |
| [RUNBOOK.md](RUNBOOK.md) | the two most likely failure modes — symptom, confirmation, recovery |
| [INCIDENT_LOG.md](INCIDENT_LOG.md) | real failures during development, with what they cost and what fixed them |
| [docs/architecture.md](docs/architecture.md) | the architecture and every deployment boundary, justified |
| [docs/agent-topology.md](docs/agent-topology.md) | the four agents, hand-offs, loop bounds, mid-run failure |
| [docs/difficult-case.md](docs/difficult-case.md) | the section 4.1 case: clauses, test values, reference answer |
| [docs/data-contracts.md](docs/data-contracts.md) | every interface, every validation rule, the quarantine format |
| [docs/retrieval.md](docs/retrieval.md) | chunking and embedding strategy, and why this corpus is hard |
| [docs/evaluation.md](docs/evaluation.md) | metrics, the prompt comparison, the before/after |
| [docs/slo.md](docs/slo.md) | the objective, declared up front, with its justification |
| [docs/load-test.md](docs/load-test.md) | measured results, the knee, the bottleneck |
| [docs/scaling.md](docs/scaling.md) | what `--scale worker=3` actually bought |
| [docs/cost.md](docs/cost.md) | tokens and spend per run and per 100 runs, largest driver |
| [docs/stub-provider.md](docs/stub-provider.md) | what the stub is for and what it cannot tell you |
| [docs/testing.md](docs/testing.md) | what the tests cover and what they deliberately do not |
| [docs/requirements.md](docs/requirements.md) | all 22 requirements, with status and where each is implemented |

---

## Troubleshooting

**`/health/ready` says `corpus_index: ok: false`, or every run escalates with
`weak_retrieval`.** The index is empty. Run `make corpus && make ingest`. Check
with `curl -s localhost:8080/health/corpus`.

**`corpus-fetch` fails with 403.** The insurer's CDN rate-limits or blocks
automated requests from some networks. Download the two PDFs listed in
`corpus/sources.yaml` by hand into `corpus/raw/` with exactly the filenames given
there, then run `make ingest`.

**Every run escalates with `provider_unavailable`.** The circuit breaker is open.
`curl -s localhost:8080/metrics | grep triage_circuit_state` — 2 means open.
Check the key and base URL in `.env`, then
`docker compose exec worker curl -XPOST localhost:9100/admin/circuit/reset`.

**`/health/ready` reports `embeddings: hash`.** The embedding model could not be
downloaded. Semantic retrieval is degraded. See
[the configuration note](#configuration); the usual cause is no network on first
run. `docker compose restart worker` once connectivity is back.

**The worker takes 40 seconds to become healthy on first start.** It is
downloading the embedding model into the `index-data` volume. It happens once.

**`docker compose up` fails on the `cadvisor` service.** cAdvisor needs
privileged access to the Docker socket and does not work on every host. It only
feeds the two per-container resource panels. Comment the service out of
`docker-compose.yml` and remove the `cadvisor` job from
`ops/prometheus/prometheus.yml`; everything else is unaffected.

More in [RUNBOOK.md](RUNBOOK.md).

---

## What is not finished

Stated plainly, because the brief says completing all 22 requirements is not
expected and sound prioritisation is itself assessed. The full list with status
is in [docs/requirements.md](docs/requirements.md); the honest summary:

- **`docker compose up` has not been run by the author.** Docker is not installed
  on the development machine this was built on. Every service, Dockerfile stage
  and compose dependency is written and reviewed, and the entire Python system is
  verified end to end outside Docker — 167 tests, the full HTTP surface, the
  agent graph, the circuit breaker, the load test and the evaluation harness all
  run and are reported here with real numbers. But the container build itself is
  unverified, and that is the first thing to check. See
  [docs/testing.md](docs/testing.md#what-is-not-verified).
- **The reported load-test and cache numbers were taken outside Docker**, in
  `EXECUTION_MODE=inline` where the API process also executes runs. The
  bottleneck identified there (a 40-thread pool) is an artefact of that
  topology, not of the queue-and-worker one. [docs/load-test.md](docs/load-test.md)
  is explicit about which numbers are which, and what to re-measure.
- **Every evaluation metric in this repository was measured against the stub
  provider** and is marked `valid_for_reporting: false`. They demonstrate that
  the harness, the metrics and the grading work. They are not a measurement of
  answer quality, which needs a real model.
- **`--scale worker=3` is reasoned about but not measured.**
  [docs/scaling.md](docs/scaling.md) states the prediction and the method rather
  than pretending to a result.
- **Guardrails are rule-based, not model-based.** That is a deliberate trade —
  explainability and cost — and its limitation is stated in
  `claimtriage/guardrails.py` and in the report.
- **TruLens is not used.** An equivalent with the same four feedback functions
  is implemented instead, for reasons given in
  [docs/evaluation.md](docs/evaluation.md#why-not-trulens).
