# Runbook

Two failure modes in detail, because they are the two that actually happen; a
short list of the rest at the end. Each one is **symptom → confirmation →
recovery**, and the confirmation step exists so that nobody applies the recovery
to the wrong problem.

Both have a Prometheus alert pointing at them.

---

## Failure mode 1 — runs queue up and p95 breaches the SLO

**Alerts:** `SLOBreach` (p95 > 45 s for 5 min), `QueueBacklog` (depth > 50 for 3 min)

### Symptom

- The Grafana panel *p95 run latency* is amber or red.
- *Queue depth* is rising and not draining.
- Callers report that `GET /v1/runs/{id}` stays `queued` for minutes.
- Submissions still return 202 immediately — the front door is fine, which is
  the design working and also what makes this easy to miss.

### Confirmation

Work through these in order. They distinguish the four causes that present
identically.

```bash
# 1. Is work actually arriving faster than it is being done?
curl -s localhost:9090/api/v1/query?query=max\(triage_queue_depth\)
curl -s localhost:9090/api/v1/query?query=triage:runs:rps
```

```bash
# 2. Are the workers alive and taking work?
docker compose ps worker
docker compose exec worker curl -s localhost:9100/health/ready
# sum of inflight across replicas; should be replicas × WORKER_CONCURRENCY
curl -s localhost:9090/api/v1/query?query=sum\(triage_inflight_runs\)
```

```bash
# 3. Is the provider the limit rather than us?
curl -s localhost:9090/api/v1/query?query=max\ by\ \(provider\)\ \(triage_circuit_state\)
curl -s localhost:9090/api/v1/query?query=sum\(rate\(triage_llm_retries_total\[2m\]\)\)
```

```bash
# 4. Are runs looping instead of answering?
curl -s localhost:9090/api/v1/query?query=triage:runs:escalation_ratio
curl -s "localhost:9090/api/v1/query?query=sum(rate(triage_run_steps_sum%5B5m%5D))/sum(rate(triage_run_steps_count%5B5m%5D))"
```

Read the answers like this:

| what you see | cause | go to |
|---|---|---|
| inflight **below** replicas × concurrency, queue rising | workers are not consuming — crashed, wedged, or Redis unreachable | **A** |
| inflight **at** replicas × concurrency, queue rising | genuinely under-provisioned | **B** |
| circuit state 2, retries climbing | the provider is the limit | **C**, then failure mode 2 |
| mean steps near `AGENT_MAX_STEPS`, escalation ratio high | runs are looping through the retry ladder | **D** |

### Recovery

**A — workers not consuming.**

```bash
docker compose logs --tail=200 worker          # look for the last log line before it stopped
docker compose exec redis redis-cli ping       # must say PONG
docker compose exec redis redis-cli llen triage:queue
docker compose restart worker
```

If the worker is being OOM-killed (`docker compose ps` shows repeated restarts,
or `State: exited (137)`), the usual cause is the embedding model inside the
2 GB limit alongside `WORKER_CONCURRENCY` threads. Either raise the limit in
`docker-compose.yml` or drop `WORKER_CONCURRENCY` to 1 and restart.

**B — under-provisioned.** Add capacity, cheapest lever first:

```bash
# threads inside each replica -- no extra container, no extra model copy
WORKER_CONCURRENCY=4 docker compose up -d worker

# then replicas
docker compose up -d --scale worker=3
```

Watch `triage:runs:rps` for 2–3 minutes. If throughput does not rise with
replicas, the bottleneck is shared and contended — work through
[docs/scaling.md](docs/scaling.md#if-scaling-does-not-help-what-is-actually-responsible),
which lists each candidate with its distinguishing signal.

**C — the provider is the limit.** Adding workers will make this *worse*: more
replicas means more concurrent requests into the same rate limit, more 429s,
more backoff. Instead:

```bash
CACHE_RUNS=true docker compose up -d worker            # if it was off
RATE_LIMIT_REQUESTS=10 docker compose up -d api        # shed at the door
```

Then go to failure mode 2.

**D — runs are looping.** The retry ladder is doing three passes per claim
because the verifier keeps rejecting the answer. This is the system being
correct and expensive. Check *why*:

```bash
# pick a looping run and read its verification notes
curl -s localhost:8080/v1/runs | python -m json.tool | head -40
curl -s localhost:8080/v1/runs/<run-id>/trace | python -m json.tool
```

If `unresolved_citations` is non-empty across many runs, the model is
paraphrasing instead of quoting — a prompt problem, not a capacity one. If
`support_score` is consistently just below `VERIFIER_MIN_SUPPORT`, the threshold
may be mis-set for the model in use. **Do not raise the threshold to make the
symptom go away** without running the evaluation set; that is how an unverified
answer gets shipped.

### Shed load deliberately, if you must

Rate limiting is the intended pressure valve and it is a cost control as much as
a capacity one:

```bash
RATE_LIMIT_REQUESTS=5 RATE_LIMIT_WINDOW_SECONDS=60 docker compose up -d api
```

Claims already in the run store are unaffected — nothing is lost, new
submissions get a 429 with a `Retry-After`.

---

## Failure mode 2 — the model provider is unavailable

**Alert:** `ProviderCircuitOpen` (`triage_circuit_state > 1` for 1 min)

### Symptom

- Almost every run comes back `escalated` with reason `provider_unavailable`.
- Every result has `"degraded": true` and a populated `degradation_path`.
- `triage_llm_calls_total{outcome="error"}` climbing, `triage_circuit_state` at 2.
- Latency may *improve*, because an open circuit fails fast. A sudden drop in
  p95 together with a spike in escalations is this, not an improvement.

**This is the system behaving correctly.** No confident answer is returned
without a model. The job is to work out why and restore it — not to make it
answer anyway.

### Confirmation

```bash
# 1. Is the circuit open, and on which replicas?
curl -s localhost:8080/metrics | grep triage_circuit_state
docker compose exec worker curl -s localhost:9100/health
```

```bash
# 2. Can the provider be reached from inside the worker's network namespace?
docker compose exec worker sh -c \
  'curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $LLM_API_KEY" "$LLM_BASE_URL/models"'
```

| response | meaning |
|---|---|
| `200` | the provider is fine; the circuit is stale — reset it |
| `401` / `403` | the key is wrong, expired, or the spend limit is hit |
| `404` | `LLM_BASE_URL` or `LLM_MODEL` is wrong — a withdrawn model gives this |
| `429` | rate limited; this is failure mode 1 case C |
| `000` / timeout | no network egress from the container, or the provider is down |

```bash
# 3. What did the failures actually say?
docker compose logs worker | grep llm_call_failed | tail -20
```

The log line carries the status and the first 300 characters of the provider's
own error, which is usually explicit about quota or model names.

### Recovery

**The provider is fine, the circuit is stale.**

```bash
docker compose exec worker curl -XPOST localhost:9100/admin/circuit/reset
```

The breaker also self-heals: it moves to half-open after
`CIRCUIT_RESET_SECONDS` (30 s) and closes on the first success. A manual reset is
for when you do not want to wait.

**Credential or quota.** Fix `.env`, then recreate the worker so the new value
is injected — this configuration is read at process start:

```bash
docker compose up -d --force-recreate worker
```

**The model identifier is wrong or withdrawn.** Free-tier model names change.
List what the provider actually offers and update `LLM_MODEL`:

```bash
docker compose exec worker sh -c \
  'curl -s -H "Authorization: Bearer $LLM_API_KEY" "$LLM_BASE_URL/models"' \
  | python -m json.tool | grep '"id"'
```

**The provider is genuinely down.** Switch providers — this is what the interface
exists for. Three environment variables and a restart:

```bash
# e.g. from an OpenAI-compatible gateway to Gemini
LLM_PROVIDER=gemini LLM_API_KEY=... LLM_MODEL=gemini-2.0-flash \
  docker compose up -d --force-recreate worker
```

**Nothing is available and claims are arriving.** Keep accepting them. The API
does not need the provider — submissions are validated and queued, and they will
be triaged when it returns. Meanwhile every claim routes to
`human_escalation` with its retrieved passages attached, which is a usable
handover. Do **not** disable the verifier or lower `VERIFIER_MIN_SUPPORT` to
force answers out.

### Clearing the backlog afterwards

Runs that escalated during the outage are terminal — they will not retry
themselves, by design. To re-triage them, resubmit with a new
`claim_reference` (the original is held by the idempotency key):

```bash
curl -s localhost:8080/v1/runs?limit=200 \
  | python -c 'import json,sys;print("\n".join(r["run_id"] for r in json.load(sys.stdin)["runs"] if r["status"]=="escalated"))'
```

### Verifying the whole path before a demonstration

The stub carries the failure injection, so this can be rehearsed:

```bash
make outage            # stub returns 503 to everything
scripts/demo.sh        # observe: escalated, degraded, provider_unavailable
make recover
make circuit-reset
```

Observed behaviour, from the run that produced [docs/load-test.md](docs/load-test.md):

```
run 1 : escalated | human_escalation | degraded=True | provider_unavailable
        path: intake_classified_from_structured_fields_only -> model_unavailable_at_planning
run 2 : escalated | human_escalation | degraded=True | provider_unavailable
        path: model_unavailable_at_intake
```

Run 1 lost the intake call, fell back to classifying from the structured fields,
and then hit the now-open circuit at the planning step. Runs 2 and 3 were
short-circuited at intake without reaching the provider at all. No confident
answer was returned, and the fallback chain is visible in the result.

---

## Shorter entries

### The corpus index is empty

**Alert:** `CorpusIndexEmpty`. Every run escalates `weak_retrieval`.
`/health/ready` reports `corpus_index.ok: false`.

```bash
curl -s localhost:8080/health/corpus | python -m json.tool
docker compose run --rm tools corpus-fetch
docker compose run --rm tools ingest
```

If `corpus-fetch` returns 403, the insurer's CDN is blocking automated requests
from your network. Download the PDFs named in `corpus/sources.yaml` by hand into
`corpus/raw/` with exactly those filenames, then `ingest`.

### Embeddings silently degraded to the hash fallback

`/health/ready` reports `embeddings: "hash"`. Retrieval still works lexically but
semantic recall is gone, so the section 4.1 case may fail.

```bash
docker compose logs worker | grep embedding_model_unavailable
docker compose restart worker      # once connectivity is restored
docker compose run --rm tools ingest --force   # re-embed in the real space
```

The re-index is required, not optional: the embedding signature is part of the
index version, and a hash-built index is not searchable in the MiniLM space.

### The index is stale

**Alert:** `CorpusIndexStale` (older than 7 days). Not urgent — a policy wording
does not change weekly — but it means nobody has checked whether the insurer has
republished.

```bash
docker compose run --rm tools corpus-fetch --force
docker compose run --rm tools ingest          # only changed documents re-embed
```

### A run is stuck at `running`

A worker died mid-run. The job is gone from the queue (`BLPOP` removes on read —
the known at-most-once gap, documented in
[docs/agent-topology.md](docs/agent-topology.md#the-known-gap-at-most-once-delivery)).

```bash
curl -s localhost:8080/v1/runs?limit=100 \
  | python -c 'import json,sys;[print(r["run_id"],r["status"],r["queued_at"]) for r in json.load(sys.stdin)["runs"] if r["status"]=="running"]'
```

The claim is not lost — it is in the run store. Resubmit it with a new
`claim_reference`.

### Redis is unreachable

`/health/ready` reports `state_backend.ok: false`. Submissions may still return
202 because the store falls back to a process-local in-memory backend with a
warning — **which the workers cannot see.** Treat it as a hard failure.

```bash
docker compose logs redis | tail -50
docker compose exec redis redis-cli ping
docker compose restart redis
docker compose restart api worker       # drop the in-memory fallbacks
```

Runs submitted during the fallback window are in the API's memory and are lost on
restart. They are visible in the API log as `submission_accepted` without a
matching `run_finished`.

### Rejections spike

`triage_quarantined_records_total` jumps. A caller has deployed against a
different version of the contract, or is sending malformed data.

```bash
curl -s "localhost:8080/v1/quarantine?limit=20" | python -m json.tool
```

Every record carries the field-level errors and the raw body. Group by `rule` to
see what changed. `GET /contracts/fnol` serves the live schema — generated from
the same models that enforce it, so it cannot have drifted.

### Guardrail activations spike

`triage_guardrail_events_total{kind="injection"}` jumps.

```bash
curl -s "localhost:8080/v1/quarantine?limit=50" \
  | python -c 'import json,sys;[print(i["rule"]) for r in json.load(sys.stdin)["records"] for i in r["issues"] if "guardrail" in i["rule"]]' \
  | sort | uniq -c | sort -rn
```

If the same pattern fires repeatedly on ordinary claim text, it is a false
positive and it is quarantining real claims from real customers. Patterns live in
`claimtriage/guardrails.py` with tests in `tests/test_guardrails.py`; add the
false-positive text to `BENIGN` there before narrowing the pattern, so it stays
narrowed.

---

## Finding one run across every service

One correlation ID follows a claim from the HTTP request through the queue into
the worker and down every agent step.

```bash
# everything about one run, reconstructed
curl -s localhost:8080/v1/runs/<run-id>/trace | python -m json.tool

# every log line for it, across all containers
docker compose logs --no-log-prefix | grep '"run_id":"<run-id>"'

# or by the caller's own correlation id
docker compose logs --no-log-prefix | grep '"correlation_id":"<cid>"'

# the distributed trace
open http://localhost:16686          # service: claim-triage-worker
```
