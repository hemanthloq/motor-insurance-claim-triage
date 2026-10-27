# Load test results (D6)

> The objective was declared before any of this was measured — see
> [slo.md](slo.md), which is a separate file written first.
>
> **Every number here was taken outside Docker**, in `EXECUTION_MODE=inline`
> where the API process also executes runs, because Docker is not installed on
> the machine this was built on. That topology has a different bottleneck from
> the queue-and-worker one, and the difference is spelled out below. What to
> re-measure in containers is at the end.

## Objective

**p95 of a complete run, submission to final result, ≤ 45 s.**
`SLO_P95_SECONDS=45`.

## Setup

| | |
|---|---|
| harness | Locust 2.32.5, `loadtest/locustfile.py` |
| what is timed | the `run_complete` sample: POST, then poll `/v1/runs/{id}` until terminal. **Not** the submission call |
| model | `llm-stub` service over HTTP, through the ordinary OpenAI-compatible adapter |
| stub latency | 350 ms + up to 150 ms jitter, per call |
| calls per run | 5 on the happy path |
| embeddings | `hash` (local, deterministic) |
| vector store | local numpy |
| state | in-process backend |
| execution | `EXECUTION_MODE=inline` — FastAPI `BackgroundTasks` |
| rate limit | enabled, 30 req / 60 s per client; the harness spreads across 1000 client ids |
| run cache | disabled for the headline numbers; measured separately |
| claims | unique per request — registration, estimate and description all vary |

The stub latency is a parameter, so a p95 is only meaningful alongside it. At
350 ms × 5 calls the irreducible model time per run is about 1.75 s.

## Results

Each row is a 60–75 s run, 0 failures.

| callers | runs | throughput (runs/s) | p50 | p95 | verdict |
|---:|---:|---:|---:|---:|---|
| 5 | 62 | 1.04 | 2.50 s | 2.60 s | **MET** |
| 20 | 249 | 4.17 | 2.60 s | 2.60 s | **MET** |
| 50 | 576 | 9.64 | 2.60 s | 2.70 s | **MET** |
| 150 | 895 | **14.96** | 6.30 s | 7.90 s | **MET** |
| 400 | 822 | 13.85 | 19.00 s | 22.00 s | **MET** |
| 600 | 592 | 7.94 | 39.00 s | 41.00 s | **MET** (barely) |
| 800 | 740 | 9.89 | 53.00 s | 57.00 s | **NOT MET** |

Submission call itself, across all runs: p50 3 ms, p95 4 ms, p99 8 ms. The
asynchronous design is doing its job — the front door stays responsive while the
back end saturates.

### The shape of it

Up to 50 callers, p95 is flat at 2.6–2.7 s and throughput rises linearly:
the system is idle between requests and every run costs only its own work.

Throughput **peaks at about 15 runs/s around 150 callers**. Past that it does not
rise — it *falls*, to 13.9 at 400 and 7.9 at 600 — while latency climbs roughly
linearly with concurrency. That is textbook saturation followed by thrashing: the
work is queueing, and past the knee the contention costs more than the added
concurrency buys.

### Where the target fails, and what is responsible

**The objective fails between 600 and 800 concurrent callers** — p95 41 s at 600,
57 s at 800. Interpolating the 45 s crossing puts it at roughly **640 callers**.

The component responsible, in this topology, is the **thread pool that executes
the background tasks**. The evidence is a single gauge taken during the 600-caller
run:

```
triage_inflight_runs  38.0
```

Pinned at 38, and it never exceeded 40 in any run. That is Starlette's default
AnyIO worker-thread limit. Every submission beyond 40 concurrent runs waits for a
thread, and the wait is the entire difference between 2.6 s and 57 s.

Little's law confirms it from the other side: 40 concurrent runs at ~2.6 s of
actual work each gives 40 / 2.6 ≈ **15.4 runs/s**, which is the measured ceiling
to within 3%. The system is doing exactly as much work as 40 slots allow and
nothing more.

What is *not* the bottleneck, in this run:

- **the model.** The stub answers in 350–500 ms and `stub_inflight` stayed in
  single digits. It is not saturated.
- **retrieval.** `triage_retrieval_duration_seconds` p95 stayed at 5–25 ms, and
  the retrieval cache ran at a **99.97% hit ratio** (21,239 hits / 7 misses) —
  the queries recur heavily across a load test.
- **the state backend.** In-process; microseconds.
- **validation.** Microseconds, and it happens before anything is allocated.

## Caching (D7)

Measured with `scratch/cache_bench.py`, 20 runs per cell, two API instances
differing only in the three cache flags.

| scenario | p50 | p95 | tokens/run | total tokens |
|---|---:|---:|---:|---:|
| cache **on**, repeated claim | **0.108 s** | **0.109 s** | **739** | 14,771 |
| cache off, repeated claim | 2.267 s | 2.368 s | 14,771 | 295,420 |
| cache **on**, unique claims | 2.210 s | 2.380 s | 14,031 | 280,611 |
| cache off, unique claims | 2.241 s | 2.398 s | 14,770 | 295,400 |

**The run cache is worth a great deal on repeated traffic and nothing on unique
traffic.** On a repeated claim it cuts p95 by **95%** (2.368 s → 0.109 s) and
tokens per run by **95%** — one of twenty runs paid for the answer and nineteen
were free. On unique claims it does nothing, which is correct: there is nothing
to hit.

**The embedding and retrieval caches buy about 5% of tokens and no measurable
latency** (280,611 vs 295,400 tokens on unique claims; p95 2.380 vs 2.398 s,
inside the noise). That is a less flattering result than "caching helps", and it
is the one we measured. The reason is that both the embedder and the index are
*local*: a hash embedding is microseconds and a numpy search over 381 vectors is
milliseconds, so there is very little to save. With `sentence_transformers`
embeddings and Chroma over a network hop, the embedding cache should matter more —
that is the measurement to retake in containers.

### Reporting correction

The first version of this measurement showed identical token counts in all four
cells, which is how a real bug was found: a cache hit replayed the originating
run's `cost`, so a cached run reported 14,771 tokens it had not spent. The
cost-per-100-runs figure was therefore inflated by exactly the amount the cache
saved — the cache's whole purpose made invisible by the way it was accounted.
`TriageResult` now carries `served_from_cache` and `cached_from_run_id`, and a
cached run reports zero cost. Recorded in [INCIDENT_LOG.md](../INCIDENT_LOG.md)
as INC-004.

## Two bugs the load test found before it produced a number

Worth recording, because both would have produced a *plausible* result.

**The first run reported "VERDICT MET" on 100% failures.** Every generated claim
carried a hard-coded policy period of 2025-04-01 to 2026-03-31 while the loss
date was derived from `now()`. Run after March 2026, every claim failed the
`loss_within_policy_period` cross-field rule, 140 of 140 submissions returned
422, and because a 422 comes back in 3 ms the run looked *fast*: p95 0.00 s,
objective met. The policy period is now derived from the loss date, and the
summary refuses to declare a verdict above a 2% failure ratio:

```
VERDICT  INVALID -- 100% of runs failed; fix the test, not the target
```

Recorded as INC-003.

The second was the cache accounting above. Both share a shape: a load test that
measures nothing can still print a number, and the number will be good.

## What these numbers do and do not transfer

**Transfers to the container topology:** the per-run work (about 2.6 s at this
stub latency), the submission-call latency, the retrieval and cache behaviour,
the token counts, and the general shape — flat, then a knee, then thrashing.

**Does not transfer:** the 15 runs/s ceiling, the 640-caller knee, and the
40-thread bottleneck. All three are artefacts of `EXECUTION_MODE=inline`. In the
compose topology the work is on a Redis list and executed by `worker`
containers, so the ceiling becomes

```
replicas × WORKER_CONCURRENCY / per-run seconds
```

— at the default 1 × 2, roughly **0.8 runs/s**, which is *twenty times lower*
than measured here. One worker with two threads is a much smaller pipe than a
40-thread pool. The knee would arrive at single-digit concurrency, and the
bottleneck would be the worker pool rather than the API's thread pool.

That is not a flaw in the design; it is why `--scale worker=3` exists and why
`WORKER_CONCURRENCY` is configurable. It does mean the headline numbers above
should not be quoted as the system's capacity.

### To re-measure in containers

```bash
# point the worker at the stub, turn off the run cache
LLM_PROVIDER=openai_compat LLM_BASE_URL=http://llm-stub:9009/v1 \
LLM_MODEL=stub-1 CACHE_RUNS=false docker compose up -d

make loadtest-headless                 # 20 callers, 3 minutes
docker compose up -d --scale worker=3  # then repeat
```

Expected, and the predictions to check against:

1. p95 at 20 callers should be **far worse** than the 2.6 s here — the queue is
   the limit, not the thread pool.
2. `triage_queue_depth` should grow without bound at 20 callers on one worker.
3. Throughput should scale close to linearly to 3 workers, because the workers
   share nothing but the index and Redis. [scaling.md](scaling.md) states what to
   do if it does not.
4. The bottleneck gauge changes from `triage_inflight_runs` (capped at 40) to
   `triage_queue_depth` (growing) plus `triage_inflight_runs` capped at
   `replicas × WORKER_CONCURRENCY`.

## Reproducing the numbers above

```bash
make dev                                  # ingests and serves on :8080
./scratch/serve_stub.sh &                 # the stub on :9009
# then an API pointed at it over HTTP, and:
locust -f loadtest/locustfile.py --host http://127.0.0.1:8081 \
       --headless -u 150 -r 25 -t 60s --csv loadtest/reports/u150
```

CSVs for every run above are in `loadtest/reports/`, and the cache measurement is
in `loadtest/reports/cache_effect.json`.
