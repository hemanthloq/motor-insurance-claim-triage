# Scaling (A7)

> **This is a prediction and a method, not a result.** `docker compose up
> --scale worker=3` has not been run — Docker is not installed on the machine
> this was built on. The commands, the metrics to read and the numbers to expect
> are below, along with what each possible outcome would mean. What *was*
> measured, outside Docker, is in [load-test.md](load-test.md).

## What `--scale worker=3` is supposed to buy

```bash
docker compose up -d --scale worker=3
docker compose ps worker
```

The worker is stateless. Everything it needs comes from the job (a run id), the
run store and the shared index. Three replicas compete on one Redis list;
`BLPOP` hands each job to exactly one of them, and nothing has to be partitioned
or coordinated. There is no host port bound on the worker precisely so that three
replicas can exist — Prometheus reaches each one through the compose DNS name
with `dns_sd_configs`.

So the expectation is near-linear throughput scaling, and it is worth being
explicit about why that is credible here and when it would not be:

| | shared? | contended? |
|---|---|---|
| Redis list (the queue) | yes | one `BLPOP` per job; trivially cheap |
| Redis (run store, caches) | yes | a handful of GET/SET per run |
| Chroma (the index) | yes | **read-only** during a run |
| the embedding model | no — one copy per replica | not contended, but ~90 MB of RAM each |
| the model provider | yes | **this is the one that bites** — see below |

## Capacity model

```
runs/second  ≈  replicas × WORKER_CONCURRENCY / per-run seconds
```

At the measured per-run work of ~2.6 s (stub at 350 ms × 5 calls):

| replicas | `WORKER_CONCURRENCY` | predicted runs/s | predicted p95 at 20 callers |
|---:|---:|---:|---|
| 1 | 2 | 0.77 | ~26 s and climbing — queue-bound |
| 3 | 2 | 2.3 | ~9 s |
| 3 | 4 | 4.6 | ~5 s |
| 1 | 2 | — | with a *real* provider at 3 s/call, per-run work is ~15 s, so 0.13 runs/s |

The last row is the one that matters operationally. With a real provider the
per-run cost is dominated by five sequential model calls, so **one worker with
two threads sustains about eight runs a minute.** A 200-claim morning takes
twenty-five minutes at three replicas and seventy-five at one. That is the
calculation that decides the replica count, and it is why the SLO work in
[slo.md](slo.md) insisted on forcing the throughput question into the open.

## The two levers, and which to reach for

There are two ways to add capacity and they fail differently.

**`WORKER_CONCURRENCY` (threads inside one replica).** Cheap — no extra
container, no extra copy of the embedding model. The work is a mixture of
blocking HTTP (the provider) and CPU (embedding a query, scoring BM25), so
threads help: while one thread waits on the provider another can embed. It is
bounded by the GIL on the CPU half and by memory on the model half. Reach for
this first, to maybe 4–6.

**Replicas.** The real lever past that, because it adds CPU and sidesteps the
GIL. Costs ~90 MB of model plus interpreter per replica, and each replica keeps
its own circuit-breaker state (deliberately — see below).

## If scaling does not help, what is actually responsible

A7 asks for the bottleneck to be identified if scaling does not help. Each
candidate has a distinguishing signal, so this is a decision procedure rather
than guesswork.

### 1. The model provider's rate limit — the most likely

**Signal:** `triage_llm_calls_total{outcome="error"}` and
`triage_llm_retries_total` climb with replica count; `triage_circuit_state`
reaches 2 on one or more replicas; latency rises while throughput stays flat.

This is the one the brief warns about: agents multiply model calls, and free
tiers have low rate limits. Three replicas × 2 threads × 5 calls per run is 30
in-flight requests, which is above several free tiers' concurrency allowance.
Scaling the workers then just redistributes the same ceiling and adds backoff.

**What it means:** the bottleneck is outside the system and no amount of local
scaling will move it. The levers are the ones already built — the run cache
(95% token saving on repeats, [load-test.md](load-test.md)), the per-client rate
limiter, and the loop bounds. This is exactly why they exist.

**Note:** this is also why the load test uses the stub. With a real provider,
A7's measurement would measure the provider's rate limiter and nothing else.

### 2. Chroma

**Signal:** `triage_retrieval_duration_seconds{strategy="hybrid"}` p95 rises
with replica count; `triage_cache_events_total{layer="retrieval",event="miss"}`
is high; the `chroma` container's CPU (on the cAdvisor panel) is saturated.

**What it means:** one index server is serving three workers' searches. Raise
`RETRIEVAL_TOP_K` down, lean on the retrieval cache, or give Chroma more CPU. At
381 chunks this is very unlikely; at 50,000 it would be the first thing to go.

### 3. Redis

**Signal:** `triage_queue_depth` oscillates rather than draining; Redis CPU
saturated; `store` operations appearing in the per-step latency.

**What it means:** unlikely at these volumes — a run is a handful of Redis
operations. If it happens, the six key namespaces are already separated so the
queue can be moved to its own instance without touching a call site.

### 4. The API tier

**Signal:** submission-call p95 (`POST /v1/claims` in Locust) rises above
~50 ms while run latency is flat.

**What it means:** add API replicas. It is stateless too. This is the easiest
bottleneck to fix and the least likely to appear, because validation is
microseconds.

### 5. Nothing is saturated and throughput is still flat

**Signal:** `triage_inflight_runs` summed across replicas is well below
`replicas × WORKER_CONCURRENCY`.

**What it means:** the workers are not being given work — the queue is empty
because the *load generator* is the limit. Raise Locust's user count. This is a
real and easily-missed outcome, which is why the inflight gauge exists.

## Method

```bash
# 1. baseline: one worker
docker compose up -d --scale worker=1
make loadtest-headless                     # 20 callers, 3 min, CSV written

# 2. three workers, identical load
docker compose up -d --scale worker=3
make loadtest-headless

# 3. read the effect
#    - throughput and p95 from the Locust CSV / the printed D6 summary
#    - queue depth, inflight runs and per-agent step latency from Grafana
```

Queries to compare, all of them on the dashboard already:

```promql
triage:runs:rps                                   # throughput
triage:run_duration_seconds:p95                   # the SLO metric
max(triage_queue_depth)                           # is work backing up?
sum(triage_inflight_runs)                         # are the slots being used?
sum by (provider) (rate(triage_llm_calls_total{outcome="error"}[2m]))
max by (provider) (triage_circuit_state)
histogram_quantile(0.95, sum by (le,strategy)
  (rate(triage_retrieval_duration_seconds_bucket[5m])))
```

The honest report is a table of the two runs with throughput, p50, p95, queue
depth and the bottleneck signal, plus the scaling efficiency:

```
efficiency = (throughput at 3 replicas) / (3 × throughput at 1 replica)
```

Above ~0.8 is good scaling. Below ~0.5, something in the list above is shared and
contended, and the signals say which.

## One deliberate non-decision: per-replica circuit breakers

Each replica keeps its own breaker rather than sharing one through Redis. The
thing being protected against is a provider that is down or rate-limiting us, and
each worker observes that independently within a second or two. A shared breaker
would add a Redis round trip to the hot path and a shared failure mode, to buy a
few seconds of earlier tripping on the replicas that have not yet seen an error
themselves.

The consequence under `--scale worker=3` is worth knowing: during an outage you
will see the breaker open on the replicas in sequence, not all at once, and
`max by (provider) (triage_circuit_state)` will read 2 before every replica has
tripped. That is expected, and it is why the dashboard panel uses `max` rather
than `avg`.
