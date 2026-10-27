# Architecture and deployment boundaries

## The shape of the system

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  INGESTION BOUNDARY                                     (api container)     │
│                                                                             │
│  POST /v1/claims                                                            │
│    1. rate limit per client          ─── 429 ──────────────────────────┐    │
│    2. parse against fnol.v1          ─── 422 + quarantine record ──────┤    │
│    3. cross-field rules              ─── 422 + quarantine record ──────┤    │
│    4. guardrails on free text        ─── 422 + quarantine record ──────┤    │
│    5. idempotency check              ─── 200 + the existing run ───────┤    │
│    6. enqueue                        ─── 202 + run_id                  │    │
│                                                                        │    │
│  NO AGENT RUNS IN THIS CONTAINER. There is no code path from a         │    │
│  rejection to a run; QuarantineRecord.agent_run_started is typed       │    │
│  Literal[False].                                                       │    │
└────────────────────────────────┬──────────────────────────────────┬────┘    │
                                 │ redis list                       │ run store
┌────────────────────────────────▼──────────────────────────────────▼─────────┐
│  EXECUTION                                        (worker container, × N)   │
│                                                                             │
│   intake_analyst                                                            │
│     └─ classify, extract entities, propose the first queries                │
│          │ hand-off: classification + retrieval plan                        │
│   coverage_analyst                                                          │
│     ├─ ground_search      ─ tool: corpus_search                             │
│     ├─ plan_modifiers     ─ decides WHETHER to retrieve again, and what     │
│     ├─ targeted_search    ─ tools: find_named_clause, corpus_search[role],  │
│     │                              clause_neighbours, get_claim_field       │
│     └─ analyse            ─ draft determination + citations                 │
│          │ hand-off: draft + the passages it was written from               │
│   verifier                                                                  │
│     ├─ deterministic: does every quote resolve to its clause?               │
│     ├─ model: per-claim entailment against those passages                   │
│     └─ verified → route │ unsupported → replan (new strategy) │ → escalate  │
│          │ hand-off: a verified determination, or an escalation             │
│   routing_officer                                                           │
│     └─ choose a queue, floored by the deterministic signals                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Where the deployment boundaries are, and why

D3 asks for the placement of deployment boundaries to be justified on scaling,
failure isolation, independent release or resource profile. Each boundary below
is there for a named reason; a boundary that only existed because microservices
are fashionable would be a liability, and the ones we considered and rejected are
at the end.

### `api` ↔ `worker` — scaling and failure isolation

The API is a validating front door. It does microseconds of work per request,
must stay available, and must never block on a model call. The worker does
seconds to a minute of work per unit and is the only process that talks to a
model provider.

*Scaling.* These have completely different capacity curves. Agent capacity is
bought in whole workers; HTTP capacity is nearly free. Fusing them would mean
adding HTTP front-ends we do not need in order to get agent throughput we do.

*Failure isolation.* A worker wedged on a provider timeout, or killed by the
2 GB memory limit while loading the embedding model, takes down one unit of
capacity. In a fused service it would take down submissions too — and a claims
intake that cannot *accept* a claim is a regulatory problem in a way that one
which cannot yet *triage* it is not.

*The asymmetry is the point.* Submissions are cheap and must always succeed.
Triage is expensive and may be delayed. Splitting them lets each fail in its own
way.

### `worker` ↔ `chroma` — resource profile and release cadence

*Resource profile.* The index is memory-resident and read-mostly; the workers are
stateless and CPU-bound in bursts. They want different limits and different
restart policies.

*Sharing.* `docker compose up --scale worker=3` has to give three workers **one**
index. If the index lived inside the worker, three replicas would hold three
copies of the same vectors and the same 90 MB model, and the memory cost of
scaling would be linear in something that should be shared.

*Independent release.* Re-indexing the corpus after the insurer republishes a
wording must not require a worker deploy, and a worker deploy must not drop the
index. The `tools` container writes to Chroma while the workers keep serving.

### `redis` — the only stateful thing in the request path

Queue, run store, quarantine, feedback, caches and rate-limit counters, in six
separate key namespaces so any of them can be moved to its own instance later
without touching a call site. It is a separate service because its loss has to be
*survivable and visible*: `/health/ready` reports it, and `store.get_backend()`
falls back to a process-local in-memory backend with a loud warning rather than
failing to start.

The fallback is for development, and the log line says so. In the compose
topology, a Redis outage means submissions are accepted into a backend the
workers cannot see — which is why the readiness probe reports it and the runbook
treats it as a hard failure.

### `llm-stub` — it stands in for a third party

D6 requires the model to be replaced by a stub for load testing. Making the stub
a *service* rather than an in-process shim is what makes the measurement
trustworthy: the worker reaches it through the ordinary OpenAI-compatible
adapter, so the HTTP hop, the connection pool, the timeout, the retry and the
circuit breaker are all on the measured path. A stub reached by a different code
path would measure something we do not ship.

It also carries the failure-injection controls for demonstration item 8
(`/admin/outage`, `/admin/latency`, `/admin/failure`), which is why it is in the
default profile rather than the load-test one.

### `prometheus` / `grafana` / `jaeger` / `cadvisor` — observability outlives its subject

If the worker is crash-looping, the dashboard that shows it crash-looping has to
still be up. That is the whole argument. cAdvisor is separate for a second
reason: per-container CPU and memory cannot be reported by the application about
its neighbours — it has to come from the container runtime.

### `web` — static assets, and same-origin requests

An nginx container serving the client and proxying `/v1`, `/health`, `/contracts`
and `/metrics` to the API. The browser therefore makes same-origin requests, and
no CORS configuration has to be correct for the demonstration to work. The API
can also serve the client itself (it mounts `/app/web` if present), so this
container is a convenience, not a dependency.

### One image, four roles

`api`, `worker`, `llm-stub` and `tools` all run from one Dockerfile with
different commands. They share the contracts, the index client, the telemetry
and the config; four images would mean four copies of the same layers and four
chances to drift apart on a dependency bump.

The split that *does* pay for itself is the dependency one, so there are two
stages. `runtime` carries the light set. `runtime-ml` adds sentence-transformers
and chromadb, which pull in torch, and is used only by the processes that embed
text. The API image is roughly a tenth the size and starts in about a second,
which is what makes a 10-second readiness probe interval reasonable.

### Boundaries considered and rejected

**A separate retrieval service.** Tempting — it would let retrieval scale on its
own. Rejected because the agent makes 5–12 retrieval calls per run, each
currently 5–25 ms in-process, and putting an HTTP hop in front of each would add
more latency than the separation could ever recover. Chroma already *is* that
service for the expensive part (the vector search); wrapping it in another one of
our own buys nothing.

**A separate validation service.** Rejected because validation must happen before
anything else is allocated, and a network hop in front of it creates exactly the
window the requirement exists to close. It is 200 lines in the API process, where
it can reject a request before a run id is issued.

**A separate indexer service, always running.** Rejected: indexing is a batch job
that runs when the corpus changes, which is a handful of times a year. It is a
one-shot `tools` container under a compose profile instead.

**Splitting the four agents into four services.** Rejected firmly. They share one
state object and hand off several times per run; four services would turn every
hand-off into a network call and a partial-failure case, in exchange for being
able to scale a step that is never independently the bottleneck. They are four
agents in one process, with the hand-offs made explicit in the graph — see
[agent-topology.md](agent-topology.md).

## The request lifecycle, end to end

1. **Submit.** `POST /v1/claims`. Rate limit, contract validation, cross-field
   rules, guardrails, idempotency. A correlation ID is generated if the caller
   did not supply one. The validated claim goes into the run store and a job
   (just the run id) onto the queue. 202 with `run_id`, `status_url` and
   `result_url`.
2. **Poll or be called back.** `GET /v1/runs/{id}` until the status is terminal.
   An optional `X-Callback-Url` header gets a POST when the run finishes.
   Polling is primary because it requires nothing of the caller — no reachable
   endpoint, no retry handling, no replay protection — and a claims handler UI is
   already polling a work queue.
3. **Execute.** A worker pops the job, loads the claim, runs the graph, and
   writes the result back into the run store. Every step is recorded.
4. **Read.** `GET /v1/runs/{id}/result` for the contract,
   `GET /v1/runs/{id}/trace` for the whole reconstruction.
5. **Feed back.** `POST /v1/feedback`. One in five completed runs is also scored
   automatically against the evaluation metrics and charted over time.

## Why asynchronous, specifically

A4 asks for this to be justified. Three reasons, and only the first is about
elegance:

1. A claims intake sits behind a call-centre application and a mobile app.
   Neither can block a user-facing thread for forty seconds.
2. A synchronous handler couples the caller's timeout to ours. The caller gives
   up at 30 s, we carry on for another 20, the work completes and nobody has the
   answer. With a run store, the answer is there whenever anyone asks.
3. Scaling. With the work on a queue, `--scale worker=3` adds capacity without
   touching the API tier, and a worker crash loses one job rather than one HTTP
   connection *and* its result.

A queue also gives the thing the cost section of the brief warns about: a place
to apply backpressure. One client looping against a multi-step agent can exhaust
a free tier in minutes. The rate limiter sheds that at the door, and the queue
depth makes it visible on the dashboard before the bill does.

## Data flow for the corpus

```
corpus/sources.yaml  ──▶  corpus-fetch  ──▶  corpus/raw/*.pdf
                                                   │
                            ┌──────────────────────▼───────────────────┐
                            │  ingest   (idempotent, keyed on sha256)  │
                            │   load → normalise → segment → chunk     │
                            │        → tag role → extract references   │
                            │        → embed (local) → upsert           │
                            └──────────┬───────────────────┬───────────┘
                                       │                   │
                                  chroma              index_meta.json
                                (vectors +            + redis key
                                 metadata)           (freshness, D2)
```

Re-running over an unchanged corpus re-parses nothing and re-embeds nothing. A
document whose hash has moved is dropped and re-indexed **on its own**, which is
the L1 requirement that re-embedding must not mean rebuilding everything. The
unit of invalidation is a source document because that is the unit in which an
insurer republishes a wording.

Details of the chunking and embedding strategy are in
[retrieval.md](retrieval.md).
