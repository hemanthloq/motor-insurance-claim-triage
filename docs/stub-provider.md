# The stub provider

Two stubs, same logic, different transport:

| | selected by | used for |
|---|---|---|
| in-process | `LLM_PROVIDER=stub` | the test suite, and a no-network demonstration |
| HTTP service | `LLM_BASE_URL=http://llm-stub:9009/v1` | the load test (D6), and failure injection |

Both share `claimtriage/llm/stub_logic.py`. The HTTP one is what D6 requires —
"a stub service that returns fixed responses after a realistic delay" — and it is
reached through the ordinary OpenAI-compatible adapter, so the HTTP hop, the
connection pool, the timeout, the retry and the circuit breaker are all on the
measured path. A stub reached by a different code path would measure something we
do not ship.

## Why it is rule-based and not constant

A stub that returns one constant string is no use: the graph would fail to parse
it at the first step, every run would escalate, and the load test would measure
the escalation path rather than the one anybody runs.

So this stub reads the `[[STEP:...]]` tag that every prompt carries, pulls the
clause identifiers out of the context block it was given, and returns a
well-formed response for that step which is *consistent with what it was shown*.
Under load the graph therefore executes the same number of steps, the same tool
calls and the same verification work as it does against a real provider — 7 steps
and 5.3 tool calls per run, matching the evaluation set.

In particular it lifts a **verbatim span** out of the context block for each
citation, so citations resolve and the happy path is exercised. Getting that
wrong is not harmless: an early version took the span from the wrong side of the
newline, so every quote failed the deterministic resolution check, every run went
round the retry ladder three times and escalated, and the load test measured the
failure path at 15 steps per run instead of 7.

## What it cannot do

It is not a model. It cannot read a policy clause. Specifically it cannot:

- reason about whether a licence was valid, or a notice was timely
- recognise a question the corpus does not answer, so it never escalates
  honestly
- read prompt text at all beyond the step tag — **so it cannot distinguish
  prompt set v1 from v2**

That last one is why `prompt-compare` refuses to run against it without
`--allow-stub`: a comparison returns a zero delta on every metric, which looks
like "the change had no effect" and is actually "nothing read the change".

Three of the 25 evaluation scenarios fail against it for exactly these reasons
(`S14` licence, `S16` late notification, `S18` out-of-corpus), and those failures
are the expected result rather than a defect.

## The honesty flags

Because a number measured against the stub is a number about the stub, three
guards exist:

1. `run_evaluation()` raises `StubProviderRefused` unless `--allow-stub` is
   passed.
2. Every report carries `"valid_for_reporting": false` and
   `"stub_override_used": true`, and the CLI prints
   `!! stub provider: these numbers are NOT valid for reporting`.
3. `prompt-compare` refuses outright and explains why.

## Failure injection

The HTTP stub carries the controls for demonstration item 8:

```bash
curl -XPOST localhost:9009/admin/outage              # every call returns 503
curl -XPOST localhost:9009/admin/recover
curl -XPOST localhost:9009/admin/latency -H 'Content-Type: application/json' \
     -d '{"latency_ms": 3000, "jitter_ms": 1000}'    # a slow provider
curl -XPOST localhost:9009/admin/failure -H 'Content-Type: application/json' \
     -d '{"failure_rate": 0.3}'                      # intermittent errors
```

`make outage` / `make recover` wrap the first two.

The outage path has been exercised end to end; the observed behaviour is in
[../RUNBOOK.md](../RUNBOOK.md#verifying-the-whole-path-before-a-demonstration) —
runs escalate `provider_unavailable`, `degraded: true`, with the fallback chain
visible in `degradation_path`, and no confident answer is returned.

`/admin/latency` is the knob that makes the load test meaningful: the measured
p95 is only interpretable alongside the stub latency it was taken at, which is
why every row in [load-test.md](load-test.md) records it.

## Metrics

The stub exports its own, so it can be ruled out as the bottleneck:

```
stub_requests_total{step,outcome}   # by step: so you can see the graph's shape
stub_duration_seconds              # including the injected delay
stub_inflight                      # if this saturates, the stub is the limit
stub_outage                         # 1 while simulating an outage
```

It uses `asyncio.sleep`, not `time.sleep`. A stub that blocked its own event loop
would become the bottleneck the load test exists to find.
