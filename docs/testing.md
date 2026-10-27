# Testing

```bash
make test        # 167 tests, about 4 seconds
```

No infrastructure required: in-memory state backend, the deterministic `hash`
embedder, a local numpy vector store in a temporary directory, and the
in-process stub provider. That is deliberate — a test suite that needs Redis and
Chroma up is a test suite people stop running. What that choice does *not* cover
is listed at the end, rather than left implicit.

## What is covered

| file | n | what it holds down |
|---|---:|---|
| `test_validation.py` | 29 | the ingestion boundary: all four layers, every cross-field rule, the quarantine record, and the invariant that a rejection never reports a started run |
| `test_retrieval.py` | 22 | text normalisation, heading detection, the section tree, cross-reference extraction, hybrid search, named-clause resolution, quote resolution, single-document re-indexing |
| `test_guardrails.py` | 35 | injection and toxicity detection, the false-positive list, the tool surface never rewriting text, unauthorised-output filtering |
| `test_agent.py` | 28 | the graph topology and hand-off order, loop bounds, strategy escalation, citation resolution as a property of the result, every degradation path, the circuit breaker, the cache key |
| `test_api.py` | 28 | every endpoint, the 202-then-poll contract, idempotency, all three rejection stages, the full trace, feedback, rate limiting, admin |
| `test_eval.py` | 25 | the dataset's own shape, every metric primitive, the metric definitions, the harness's stub refusal, report comparison direction |

### The tests that matter most

A handful of these are not ordinary unit tests — they hold down properties the
whole system's trustworthiness rests on.

**`test_rejection_never_reports_a_started_agent_run`** — demonstration item 4 as
an invariant rather than a screenshot. Four malformed bodies, each asserted to
produce `agent_run_started: False`.

**`test_every_returned_citation_resolves`** — A3. For two different claims, every
citation on the result is re-checked against the index. "Every citation must
resolve to retrieved text" is a property of the response object, not a prompt
instruction.

**`test_completed_runs_are_always_verified`** — an unverified answer never comes
back as `completed`. If verification fails the run escalates.

**`test_tool_surface_never_rewrites_text`** — asserts the guardrail returns
byte-identical text. Exists because an earlier version rewrote it, which silently
broke every quote taken from a rewritten passage. INC-002.

**`test_heading_is_not_absorbed_into_the_paragraph_above`** — contains the exact
trailing-space input from INC-001, the bug that filed every clause under the
wrong chapter while reporting success.

**`test_escalating_is_not_a_hallucination`** — the metric must not punish
declining to answer, or the cheapest way to score well becomes answering
everything confidently.

**`test_restricted_use_is_a_warning_not_an_error`** — the validator flags the
section 4.1 case and hands it on. If it rejected it, the agent would never see
the case the project is built around.

**`test_retry_uses_a_different_strategy_each_time`** — no search strategy is used
twice in one run. Retrying identically is how a system convinces itself.

**`test_compare_reports_reads_the_direction_of_each_metric_correctly`** — three
metrics are better when higher and one when lower. Getting that backwards makes
a regression look like an improvement.

**`test_every_scenario_patch_merges_into_a_valid_or_deliberately_invalid_fnol`** —
walks all 25 evaluation scenarios and asserts each one either validates or is
rejected by *something*. Stops a scenario silently failing for a reason it was
not written to test.

## The synthetic corpus

The real 46-page PDF is not committed (see `corpus/sources.yaml`), so the tests
cannot depend on it being present. `tests/conftest.py::synthetic_corpus`
reproduces the *structure* the system relies on in about thirty lines: a grant in
one section, a general exception in another that names a restriction by name, the
restriction itself in a separate chapter, a conditions chapter and a claim
procedure chapter.

That is enough to test every structural property — section paths,
cross-references, role tagging, named-clause resolution, quote resolution — while
being readable in one screen. What it cannot test is behaviour that depends on
the real document's scale and noise, which is what the evaluation set is for.

## What is not verified

Stated plainly rather than left to be discovered.

**`docker compose up` has never been run.** Docker is not installed on the
machine this was built on. Every service, Dockerfile stage, health check and
dependency condition is written and reviewed, and the entire Python system is
verified outside Docker — but the container build, the image layering, the
inter-service DNS and the health-check commands are unexercised. This is the
first thing to check, and the most likely place for a trivial error.

**Redis and Chroma.** The tests use the in-memory backend and the local numpy
store. `RedisBackend` and `ChromaVectorStore` implement the same interfaces and
are exercised by nothing. The fallback logic that selects them *is* tested, which
is cold comfort.

**The queue path.** Tests and the local load test run `EXECUTION_MODE=inline`,
where the API executes runs in a background task. The worker's dequeue loop,
`BLPOP` semantics and the admin port are unexercised by automated tests, though
the worker's own code paths (`process_job`) are the same ones inline mode calls.

**Real model behaviour.** Everything runs against the stub, which cannot read a
clause. So: no test asserts that the system reaches the *right* coverage
position on the section 4.1 case for the right reason — only that it finds and
cites the right clauses and does not fast-track it. The evaluation set is the
instrument for the rest, and it needs a provider.

**OpenTelemetry export.** `setup_tracing` is a no-op without a collector
endpoint, and the tests run with `TRACING_ENABLED=false`. Spans are created in
code and never asserted on.

**cAdvisor.** Needs privileged access to the Docker socket and does not work on
every host. It feeds only two dashboard panels.

**Load and scaling in containers.** See
[load-test.md](load-test.md#what-these-numbers-do-and-do-not-transfer) and
[scaling.md](scaling.md) — both are explicit about which numbers were measured
and which are predictions.

## Running pieces

```bash
make test
.venv/bin/python -m pytest tests/test_agent.py -q
.venv/bin/python -m pytest -k "citation or verif" -v
make lint                                  # compile-check every module
scripts/smoke.sh                           # the ten demonstration paths, live
```

`scripts/smoke.sh` is the complement to the unit tests: it exercises the
demonstration items against a running system and prints PASS or FAIL for each.
Against the in-process stub it scores 23 of 25 checks, the two failures being
the out-of-corpus escalation (needs a model) and the stub-service outage (needs
the service running). Run it before any demonstration so nothing is discovered
live.
