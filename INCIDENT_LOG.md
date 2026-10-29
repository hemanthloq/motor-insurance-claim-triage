# Incident log

Real failures during development. Each entry records what the symptom was, what
it actually turned out to be, what it cost, and what changed so it cannot recur
silently. They are here in the order they were found.

The common thread in six of the eight is worth stating up front: **every one of
them produced a plausible-looking result rather than an error.** A heading swallowed
into a paragraph, a guardrail quietly rewriting a quote, a load test submitting
nothing, a cache inflating its own cost report — all of them would have been
reported as successes by anyone looking at the top-line output. That is the
failure mode this kind of system actually has, and it is the argument for the
deterministic checks and the honesty flags that each fix added.

---

## INC-001 — Every clause filed under the wrong chapter

**When:** during corpus ingestion, first working index
**Severity:** high — it silently broke the case the whole project is built around
**Detected by:** eyeballing `cli search` output while checking the section 4.1 clauses

### Symptom

The index built cleanly: 357 chunks, no errors, sensible role distribution,
`ingest_complete` logged. Searching for `hire or reward` returned the right text.
But the `section_path` on it was:

```
CHAPTER 3- INDIA MOTOR TARIFF - ENDORSEMENTS > TWO WHEELERS, MOTOR VEH...
```

when the clause is in Chapter 4. The *Limitations as to Use* heading did not
exist anywhere in the index, and the General Exceptions clause was attributed to
`SECTION II - LIABILITY TO THIRD PARTIES`.

### Cause

Two independent faults compounding.

The line-joining heuristic in `normalise()` tested whether the character
immediately before a newline was sentence-final. The PDF emits a **trailing
space** after the preceding sentence, so the character before the newline was
`' '`, the test failed, and `CHAPTER 4 - Limitations as to Use` was joined onto
the paragraph above it. The chunker then never saw a heading.

Separately, `PRIVATE CAR PACKAGE POLICY WORDING` appears at the top of all 46
pages. The all-caps heading pattern matched it once per page, so it became the
dominant heading in the document and absorbed everything under it.

### Why it was dangerous

Nothing failed. There was no exception, no warning, and the retrieved *text* was
correct — only its attribution was wrong. A citation would have quoted the right
words and pointed at the wrong chapter and page, which is worse than no citation
at all for a system whose output is "here is the clause and where to find it".

### Fix

- `loaders._should_join` rewritten to decide per line pair on four explicit
  rules, including "the previous line is a `CHAPTER`/`SECTION` heading", which
  rarely ends in a full stop.
- `loaders.strip_running_lines` added: any line appearing on ≥30% of pages is
  page furniture and is removed before chunking.
- All-caps headings accepted only when the line starts a block.
- `CHAPTER`/`SECTION` headings require a separator *and* `_is_title_like`, which
  counts the capitalised share of content words — without it the pattern also
  swallowed mid-sentence cross-references like *"Section II-I(C) but subject
  otherwise to the terms exceptions"*.

### Guarded by

`tests/test_retrieval.py::test_heading_is_not_absorbed_into_the_paragraph_above`
contains the exact trailing-space input. Plus
`test_running_headers_are_stripped`,
`test_limitations_chapter_is_its_own_section`,
`test_general_exception_records_its_cross_reference`.

### Cost

About two hours, most of it spent assuming the problem was embedding quality
because the retrieved text looked right.

---

## INC-002 — The tool-surface guardrail was rewriting the text it was protecting

**When:** after the first full evaluation run against the stub
**Severity:** high — it would have made every affected run escalate for a
reason that looked like a retrieval failure
**Detected by:** log noise. `guardrail_triggered` firing on nearly every retrieval

### Symptom

Running the evaluation set produced a wall of warnings:

```
guardrail_triggered  kind=injection  pattern=tool_coercion  action=neutralise
  excerpt=55. IMT. 52. EXCLUSION OF DAMAGE WHILE IN USE AS A TOOL OF TRADE
```

### Cause

Two faults, one obvious and one not.

The obvious one: the `tool_coercion` pattern was
`\b(call|invoke|run|execute|use)\b[^.\n]{0,20}\b(tool|function|command|shell|endpoint)\b`,
which matches `USE AS A TOOL OF TRADE` — a genuine heading in the policy wording.
It fired on most retrievals.

The one that mattered: on firing, `check_tool_output` called `neutralise()` and
returned **rewritten corpus text**, which the agent then saw and quoted. The
verifier checks each quote character-for-character against the clause *as stored
in the index*. So any quote taken from a rewritten passage could not resolve, the
run would fail verification, loop through all three search strategies, and
escalate — and the trace would read like a retrieval problem.

### Fix

- Pattern narrowed to require an imperative construction
  (`call|invoke|execute` + `the? tool|function|command|endpoint`, or
  `use the X tool`).
- `check_tool_output` **no longer rewrites text, ever.** It records every
  finding, and for a high-confidence finding (≥0.9) it returns
  `allowed=False` so the caller drops the whole passage. Dropping is safe — a
  passage the agent never sees is one it cannot cite. Altering is not.
- `tools._sanitise` drops the passage and removes it from `ctx.retrieved`.

### Guarded by

`tests/test_guardrails.py::test_tool_surface_never_rewrites_text` asserts the
returned text is byte-identical to the input.
`test_tool_of_trade_heading_is_not_an_injection` holds the exact false positive.
`BENIGN` in that file is a list of real policy and claim text that must never
trigger, so narrowing stays narrowed.

### Cost

About forty minutes. The guardrail was working as written; what was wrong was
what "working" had been taken to mean on that surface.

---

## INC-003 — The load test declared the objective met on 100% failures

**When:** first load-test run
**Severity:** medium — it produced a *passing* result from a test that tested
nothing
**Detected by:** the failure count in Locust's own output, not by the verdict

### Symptom

```
runs            140   failures 140
throughput      2.39 runs/s
p50             0.00s
p95             0.00s
objective       45.00s
VERDICT         MET
```

### Cause

`build_fnol()` hard-coded `policy_start_date: 2025-04-01` and
`policy_end_date: 2026-03-31`, while deriving `date_of_loss` from `now()`. Run
after March 2026, every generated claim failed the `loss_within_policy_period`
cross-field rule and came back 422.

Because a 422 is returned in about 3 ms, the run looked *fast*. The `_summarise`
listener compared p95 against the objective, found 0.00 ≤ 45, and printed MET.

The ingestion boundary was working perfectly. That was the problem: it was the
only thing being exercised.

### Fix

- The policy period is derived from the loss date
  (`loss - 200 days` to `loss + 160 days`), with a comment saying why a fixed
  period is a time bomb.
- `_summarise` refuses to issue a verdict above a 2% failure ratio:

```
VERDICT  INVALID -- 100% of runs failed; fix the test, not the target
```

### Cost

Twenty minutes, and it would have been zero if the summary had refused to
conclude in the first place — which is exactly the change that was made.

---

## INC-004 — The cache made its own benefit invisible

**When:** measuring the D7 caching effect
**Severity:** medium — it inflated the cost report by exactly the amount the
cache saved
**Detected by:** an impossible number in the benchmark table

### Symptom

Four cache configurations, and all four reported **identical** tokens per run:

| scenario | p95 | tokens/run |
|---|---|---|
| cache on, repeated | 0.174 s | 14,771 |
| cache off, repeated | 2.482 s | 14,771 |

A run served from cache makes no model calls. It cannot have spent 14,771
tokens. The latency showed a 93% improvement while the cost showed none, which
is not a possible combination.

### Cause

On a cache hit, `execute_run` returned the cached `TriageResult` with its `cost`
field untouched — the token count of the run that originally produced the answer,
replayed as though this run had spent it. `triage:cost_per_100_runs_usd` was
therefore computing cost over *all* runs including the free ones, inflating the
figure by precisely the saving.

### Fix

- `TriageResult` gained `served_from_cache: bool` and
  `cached_from_run_id: str | None`.
- A cache hit now zeroes `cost`, `steps` and `tool_calls`, and sets both new
  fields. The result is honest about what it is: a replay, and free.

Measured properly, the run cache cuts tokens per run from 14,771 to 739 on
repeated traffic — a 95% saving, which is the number the requirement asks for and
which the bug had hidden entirely.

### Cost

Fifteen minutes, and it only surfaced because the benchmark printed latency and
cost side by side. Either column alone looked fine.

---

## INC-005 — The largest cost driver was reported as the cheapest step

**When:** writing up the per-step cost attribution
**Severity:** low, but it made an L5 answer wrong by a factor of six
**Detected by:** the per-step token table disagreeing with `largest_driver`

### Symptom

```
largest driver     intake_classification
tokens by step:
   coverage_analysis         4976
   verification              4484
   query_formulation         4140
   routing                    936
   intake_classification      854   <-- reported as the largest driver
```

### Cause

`largest_driver` was `max(per_step, key=per_step.get)` over per-step **spend**.
On a free tier every price is zero, so every per-step figure is `0.0`, and
`max()` over equal values returns whichever key is first in insertion order —
which is `intake_classification`, the first step to run and the cheapest of the
five.

### Fix

- `CostBreakdown` gained `tokens_per_step` and `largest_driver_basis`.
- The driver is read off spend when any price is configured and off **tokens**
  otherwise. Tokens are the invariant unit: they identify the same step whatever
  the price list says, and they are what a rate limit is denominated in.

The corrected answer — `coverage_analysis`, 32% of the run — turned out to be
the useful one. The three expensive steps are exactly the three that receive the
retrieved context block, which is serialised into the prompt three times per
run. That is a 25% token saving available for a small change, and it is noted in
[docs/cost.md](docs/cost.md) as not taken.

### Cost

Ten minutes. Worth more than it cost: the wrong answer pointed at an irrelevant
optimisation, the right one pointed at a real saving.

---

## INC-006 — Two tests encoded assumptions the system did not make

**When:** first full test run
**Severity:** low — tests failing, system correct
**Detected by:** `pytest`

### Symptom

```
FAILED test_agent.py::test_circuit_breaker_opens_and_then_short_circuits
  CircuitOpen: circuit open for provider broken; retry in 29.3s
FAILED test_agent.py::test_a_search_step_records_what_it_retrieved
  assert all(s.top_score is not None for s in searches)
```

### Cause

Both were wrong assumptions in the tests, not defects.

The breaker counts **attempts, not logical calls.** With `LLM_MAX_ATTEMPTS=3`,
one `complete()` against a dead provider sends three real requests and records
three failures, so a threshold-of-2 breaker opens part-way through the *first*
call and that call surfaces as `CircuitOpen` rather than `ProviderError`. The
test expected two clean `ProviderError`s first.

The second test asserted that every step recording clause ids also records a
retrieval score. The `coverage_analysis` step records the clause ids it chose to
**cite** — those came from the analyst, not from a ranker, so there is no score
to report.

### Fix

Both tests corrected to match the real contract, and the real contract
documented where it was previously implicit: the attempt-counting semantics are
now in the `CircuitBreaker` docstring, including the consequence that a call can
raise `CircuitOpen` having genuinely reached the provider. Two further tests
added for the half-open transition and for closing on success.

### Why it is logged

Because the temptation was to "fix" the breaker to count logical calls so the
test would pass. The behaviour is defensible — the thing being rationed is
requests to the provider, and grouping them inside one of our function calls does
not make them cheaper for the provider — so the right change was to write the
behaviour down, not to alter it to match an assumption nobody had checked.

### Cost

Fifteen minutes.

---

## INC-007 — The same date bug as INC-003, in a second place

**When:** first time the web client was opened by someone other than its author
**Severity:** medium — the demonstration page could not complete a single claim
**Detected by:** the user clicking Submit

### Symptom

Every submission from the web client came back rejected:

```
REJECTED AT THE INGESTION BOUNDARY
No agent run started. Stage: cross_field.
  date_of_loss | loss_within_policy_period
  date_of_loss falls outside the policy period 2025-04-01..2026-03-31
```

Not one scenario button worked. The page could show the rejection path and
nothing else.

### Cause

`web/index.html` defaulted the date of loss to `now() - 2h` while hard-coding
the policy period as `2025-04-01` to `2026-03-31`. Every claim submitted after
31 March 2026 therefore fell outside cover and was correctly rejected by the
`loss_within_policy_period` cross-field rule.

**This is INC-003 exactly, in a different file.** The load test had the same
defect — a dynamic loss date against a literal policy period — and when it was
fixed there, nobody grepped for the other places the same pair of literals
appeared. There were three: `loadtest/locustfile.py` (fixed at the time),
`scripts/_make_fnol.py` and `eval/dataset.yaml` (both safe, because they fix
the loss date *too*, so the pair stays consistent), and `web/index.html`
(broken, because it mixed one dynamic value with two static ones).

### Why it survived every check

The test suite, `demo.sh`, `smoke.sh` and the evaluation set all run against
`_make_fnol.py` or `dataset.yaml`, where both dates are literals and therefore
consistent. **Nothing automated submits what the web client submits.** 167
tests, 25 scenarios and a 23/25 smoke run all passed on the day the page was
unusable.

### Fix

- The policy period is derived from the date of loss
  (`dol - 200 days` to `dol + 160 days`), as the load test already did.
- The derived period is displayed under the date field, so a submission can no
  longer be silently coupled to a rule the user cannot see.

### Cost

Ten minutes to fix. It had been broken since 31 March 2026 and was found by
the first person to click the button.

### What it says about the testing

The honest reading is in [docs/testing.md](docs/testing.md#what-is-not-verified):
the web client is listed there as unverified, and it was. A page that no test
exercises is a page that is broken until someone opens it. The cheap guard
would be a single smoke check that submits the exact body the form builds —
not added, and recorded here rather than claimed.

---

## INC-008 — The fix for INC-007 killed the submit button

**When:** immediately after shipping the INC-007 fix
**Severity:** high — the demonstration page did nothing at all
**Detected by:** the user clicking Submit and getting no response whatsoever

### Symptom

The page rendered perfectly. Every field, every scenario button, the layout,
the footer. Clicking **Submit** did nothing: no request, no error, no change to
the output panel. Silence.

### Cause

The INC-007 fix added two module-level constants:

```js
const DAY = 86400000;
const isoDate = (d) => new Date(d).toISOString().slice(0, 10);
```

placed next to `buildFNOL()`, which is **below** the `(function init(){...})()`
that runs on load — and `init()` calls `showPeriod()`, which uses both. A
`const` is in the temporal dead zone until its declaration is reached, so
`init()` threw `ReferenceError: Cannot access 'DAY' before initialization`.

That throw aborted the rest of the script. `$("form").onsubmit = ...` sits 50
lines further down and never executed, so the form had no handler. A dead
button, and nothing on screen to say why.

### Why it was worse than the bug it replaced

INC-007 at least *told the user something* — it rendered a quarantine record.
This failed silently. The only evidence was in a browser console nobody had
open.

### Fix

- The two constants moved to the top of the script, above everything that uses
  them.
- `window.onerror` and `unhandledrejection` handlers now render the error into
  the output panel. A script error must not leave a dead page with no
  explanation.
- `scripts/check-web-client.js`: stubs enough DOM to execute the page's script
  outside a browser, then asserts the script runs to completion, a submit
  handler is attached, and `buildFNOL()` produces a body that satisfies the
  cross-field rules. Verified against both historical defects — reintroducing
  INC-007 or INC-008 makes it fail with the specific reason.
- Wired into `scripts/smoke.sh` as check 1b, skipped with a notice when node is
  absent.

### Cost

Twenty minutes, and it was shipped to the user before being caught — the second
time in a row that a web-client change went out unexercised. That is what the
checker is for, and it is the guard INC-007 said was missing and did not add.

---

## Open, not yet an incident

Things known to be wrong or unverified, recorded here so they are not discovered
as incidents by someone else.

| | |
|---|---|
| **`docker compose up` has never been run.** Docker is not installed on the development machine. Every service and Dockerfile stage is written and reviewed; the container build is unverified. | first thing to check |
| **At-most-once queue delivery.** `BLPOP` removes a job on read, so a worker dying mid-run loses the job. The claim is not lost — it is in the run store and visible in `GET /v1/runs` — but it needs resubmitting. Redis Streams with consumer groups is the fix. | [agent-topology.md](docs/agent-topology.md#the-known-gap-at-most-once-delivery) |
| **The load-test numbers were taken in `EXECUTION_MODE=inline`.** The 40-thread bottleneck found there is an artefact of that topology, not the queue-and-worker one. | [load-test.md](docs/load-test.md#what-these-numbers-do-and-do-not-transfer) |
| **The L2 prompt comparison has not been run against a real model.** The stub cannot read prompt text, so it returns a zero delta on every metric. `prompt-compare` now refuses to run against the stub rather than print the zeros. | [evaluation.md](docs/evaluation.md#result-against-the-stub-no-result) |
| **The verifier's context is not trimmed to cited passages.** ~25% of tokens per run, for a small change. | [cost.md](docs/cost.md) |
