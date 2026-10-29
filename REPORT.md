# Motor Insurance Claim Intake and Triage — Report

**Course** UE24AM342AA5 — ML System Design and AgentOps · Mini Project 01
**Domain** General Insurance · **Corpus** ICICI Lombard Private Car Package Policy

---

## 1. Problem and scope

Every motor insurer in India runs a claims intake pipeline. A customer files a
First Notice of Loss — mostly free text, arriving through a call centre, an app
or a web form. A claims handler then does four things: checks the submission is
complete and consistent, works out what kind of claim it is, looks up the policy
wording to see what is covered, and routes the claim.

This service does the first three and recommends the fourth. It is **advisory by
design**: it never approves, rejects or values a claim, and the response says so
on every run. A human claims handler decides. Getting the coverage position
wrong is a regulatory and financial problem, so the system is built around a
second, less obvious requirement — knowing when *not* to answer.

**In scope:** FNOL ingestion against a published contract, boundary validation
with quarantine, claim classification, clause retrieval from the insurer's own
published wording, a coverage position with clause-level citations,
self-verification, routing with a stated reason, and escalation to a human when
the corpus cannot support an answer.

**Out of scope**, per the brief: model training, cloud deployment, and
settlement amount calculation. The last is not a technical omission — the
service is advisory, and an output guardrail actively prevents it from stating a
settlement figure even if a model drafts one.

### The case the design is built around

> *"I was making a food delivery run in my own car when the accident happened."*

Search that and you get the own-damage cover, which grants the claim. The
provision that decides the outcome is in a different chapter, is about the
*purposes* a vehicle may be used for, and shares almost no vocabulary with what
the customer said:

> **Chapter 4 — Limitations as to Use.** The Policy covers use of the vehicle for
> any purpose other than: Hire or Reward, **Carriage of goods** (other than
> samples of personal luggage), Organised racing, Pace Making, Reliability trails
> or Speed testing, any purpose in Connection with Motor Trade.

and a third clause in a second place connects the two:

> **Chapter 2, General Exceptions III(a).** The Company shall not be liable …
> whilst the vehicle insured herein is being used otherwise than in accordance
> with the **'Limitations as to Use'**.

A single embedding search over the customer's words returns none of this. That
observation drove most of the design. Full treatment, with the test values and
the reference answer, in [docs/difficult-case.md](docs/difficult-case.md).

---

## 2. Agent design

Four agents, one shared state object, explicit hand-offs, three independent loop
bounds. LangGraph. Full detail in
[docs/agent-topology.md](docs/agent-topology.md).

```
START → Intake Analyst → Coverage Analyst ⇄ Verifier → Routing Officer → END
                              │                 │
                     ground_search          retry with a
                     plan_modifiers         *different strategy*
                     targeted_search              │
                     analyse                 or escalate → END
```

| agent | owns | may not |
|---|---|---|
| Intake Analyst | claim type, entities, the declared-use discrepancy, the first queries | touch coverage, routing or the corpus |
| Coverage Analyst | the plan, the draft, all tool use | route, or judge its own answer |
| Verifier | the verdict, and the sole authority to send work back | write or change a determination |
| Routing Officer | the queue | revisit the coverage position |

### Why this is four agents and not four prompts

The Verifier is what makes this a topology. It is **adversarial by
construction**: it receives the passages and the draft, *not* the Analyst's
reasoning, and it is the only node that can loop. Its prompt opens by telling it
so. The Routing Officer is separate for a different reason — part of its
decision is not the model's at all: deterministic signals computed from the
validated structured fields set a *floor*, and `signals.enforce()` raises the
model's choice to it, recording the override in the result. A model may be more
careful than the rules, never less.

### Tools (five)

`corpus_search(query, role, top_k, strategy)`, `find_named_clause(name)`,
`get_clause(id)`, `clause_neighbours(id, window)`, `get_claim_field(path)`.

The second one exists specifically for the section 4.1 case. The general
exception *names* the restriction; the chunker records that cross-reference at
index time; so the agent can read one clause, see what it points at, and fetch it
**by name** rather than hope a vector search lands on it. That is how a claims
handler actually works through a wording.

Retrieval is a tool the agent chooses, not a stage. `plan_modifiers` decides
whether anything could displace the grant and names what to fetch; if it answers
"nothing applies", no second retrieval happens.

### Verification

Two checks, cheap one first:

1. **Deterministic.** Does every quote appear character-for-character in the
   clause it is attributed to? No model is needed for that and none should be
   trusted with it. A citation that does not resolve is a fabrication whatever a
   verifier says.
2. **Model.** Per-claim entailment against the passages that were available,
   returning the specific statements that failed and any clauses in conflict.

Then, in the result assembler, **only resolving citations are carried through.**
"Every citation must resolve to retrieved text" is therefore a property of the
response object rather than an instruction in a prompt — and it is tested as one.

### Loop bounds

Steps (14), tool calls (12), wall clock (120 s), all configurable, all checked
independently. A run that hits a bound escalates with `loop_budget_exhausted`
and names the bound. It does not return a confident answer.

Retrying uses a **different strategy each time**: analyst-authored queries, then
a role-filtered sweep for anything that restricts or excludes, then neighbour
expansion plus a lexical-only pass over the policy's terms of art. Retrying
identically is how a system convinces itself.

---

## 3. Architecture

Ten services under Compose. The diagram and the full justification of every
boundary are in [docs/architecture.md](docs/architecture.md); the deployment
boundaries, marked:

```
┌─ api ──────────────┐   HTTP, validation, quarantine, rate limit, enqueue.
│                    │   Scaling + failure isolation: must stay up, must never
└────────┬───────────┘   block on a model call.
         │ redis list ── the queue. Backpressure lives here.
┌────────▼───────────┐   Agent execution. Seconds-to-a-minute per unit.
│  worker  × N       │   The only process that talks to a model.
└────────┬───────────┘
         │
┌────────▼───────────┐   Resource profile + release cadence: memory-resident,
│  chroma            │   read-mostly, shared by all replicas. Re-indexing must
└────────────────────┘   not need a worker deploy.

┌─ llm-stub ─────────┐   Stands in for a third party, reached through the *same*
└────────────────────┘   adapter, so the load test measures the real path.

┌─ prometheus · grafana · jaeger · cadvisor ─┐  Observability must outlive the
└────────────────────────────────────────────┘  thing it observes.

┌─ web (nginx) ──────┐   Static client + same-origin proxy.
└────────────────────┘
```

**Asynchronous, for three reasons.** A call-centre application cannot block a
user-facing thread for forty seconds. A synchronous handler couples the caller's
timeout to ours — they give up at 30 s, we finish at 50, and nobody has the
answer. And with the work on a queue, `--scale worker=3` adds agent capacity
without touching the API tier, and a worker crash loses one job rather than one
HTTP connection *and* its result.

**One image, four roles**, two Dockerfile stages. The split that pays is the
dependency one: `runtime-ml` adds sentence-transformers and chromadb (and
therefore torch) and is used only by processes that embed text, so the API image
is about a tenth the size and starts in a second.

---

## 4. Design decisions, and what was rejected

### Structure-aware chunking over a fixed-size splitter

A policy wording is a tree — chapters, sections, roman clauses, lettered
sub-clauses — not prose. A fixed-size splitter cuts across it and produces a
chunk containing the back half of an exclusion and the front half of a
definition. Retrieve that and you cite text which does not say what you claim it
says, which for a system whose entire output is "here is the clause" is the worst
available failure.

So: three passes. Repair the extracted text, segment on the document's own
hierarchy, then pack into chunks without crossing a heading — and tag each chunk
with a *role* and the names of the clauses it cross-references.
[docs/retrieval.md](docs/retrieval.md).

**Rejected:** recursive character splitting (cuts clauses in half);
LangChain/LlamaIndex retrieval chains (the chunker had to be hand-written for
this corpus anyway; wrapping it would add an abstraction and change no logic —
this is the one place the letter of L1 is not met, and it is flagged as such in
[docs/requirements.md](docs/requirements.md#note-on-l1-and-the-orchestration-framework)).

### Hybrid dense + BM25, fused with RRF

Policy wordings turn on exact terms of art — *hire or reward*, *pace making*. A
384-dimension embedding smears those into a neighbourhood of
generally-insurance-ish text; BM25 matches them precisely. Conversely *"I was
dropping off a food order"* shares no tokens at all with the clause that governs
it, and only the dense side bridges that. **Neither half is sufficient and their
failure modes are complementary.**

RRF rather than a weighted sum, because cosine is bounded in [−1, 1] and BM25 is
unbounded and corpus-dependent, so there is no principled weight to choose on a
corpus this size.

**Rejected:** dense-only (misses the terms of art, which is the whole case);
lexical-only (misses the customer-to-policy vocabulary gap); a cross-encoder
reranker (a second model call per query, on a free tier, for a candidate set of
six).

### Local embeddings

`all-MiniLM-L6-v2`, 384 dimensions, ~90 MB, no key, no per-token cost. The brief
is right that embedding a 46-page wording through a paid API is the only
operation here with a real bill attached.

A deterministic `hash` fallback exists so the service still starts when the model
cannot be downloaded. It is genuinely useless for semantic similarity, so
`/health/ready` reports which backend is live — a degraded mode that cannot be
mistaken for a working one is worth more than one that silently is not.

### Rule-based guardrails

**A deliberate trade, and the most arguable decision in the project.** A
model-based detector would catch more paraphrases. It would also add a model
call per field to a pipeline already making five to eleven per run, on a free
tier with a low rate limit, and make a block unauditable — *"the classifier said
0.71"* is not something a compliance reviewer can act on. These rules catch the
direct-instruction family, which is what actually appears in claims text, and
every block names the exact pattern that fired.

The limitation is real and is stated in the module docstring: a sufficiently
indirect injection gets through. The three-surface structure (input, tool,
output) matters more than the detector's sophistication, and the **output**
filter is the one that makes the system safe regardless — it is what stops an
unauthorised determination leaving the process no matter how it got drafted.

### The validator flags restricted use; it does not decide

The most consequential line in the boundary, and easy to get backwards. A private
car used for food delivery is a **perfectly valid claim to triage** — whether a
delivery run is "hire or reward" turns on facts the FNOL does not contain. If the
validator rejected it, the agent would never see the section 4.1 case, and the
system would be making a coverage determination in a regex.

So: **the validator decides whether the submission is well-formed; the agent
decides what the policy says.**

### Polling over callbacks

Primary mechanism is polling, because it requires nothing of the caller — no
reachable endpoint, no retry handling, no replay protection — and a claims
handler UI is already polling a work queue. A callback is available for batch
integrations via `X-Callback-Url`, delivered best-effort: a failed callback must
not fail a run whose work is already done and stored.

### At-most-once queue delivery

`BLPOP` removes a job on read, so a worker dying mid-run loses the job. Redis
Streams with consumer groups would give at-least-once and reclaim — and
executing a triage run twice is harmless, since it is advisory and read-only, so
that is the right next change. What made it wrong *now* is that the claim is
never lost: it is in the run store, visible in `GET /v1/runs`, and a stuck run is
detectable. Documented as a known gap with a runbook entry rather than hidden.

---

## 5. Evaluation results

> **Every number in this section was measured against the stub provider** and is
> marked `valid_for_reporting: false` in the report it came from. The stub cannot
> read a policy clause. These demonstrate that the harness, the metrics and the
> grading work — they are not a measurement of answer quality. Full discussion:
> [docs/evaluation.md](docs/evaluation.md).

25 scenarios, each with its expected outcome and supporting passage. Prompt set
v2, `hash` embeddings:

```
scenarios 25   passed 22   failed 3   pass rate 88.0%
rejected at the boundary 6 (all 6 with no agent run started)
agent runs 19   mean 7.0 steps, 5.3 tool calls, 2.14 s

groundedness        0.3421
context relevance   0.0057
faithfulness        1.0000
hallucination rate  0.0000
```

**What this shows:** the boundary rejects all six scenarios that must be
rejected; every citation returned resolves (faithfulness 1.0); nothing is
asserted without support (hallucination 0.0); the section 4.1 reference case
passes its position, queue and citation assertions.

**What it does not show:** reasoning quality. The three failures are all things
the stub cannot do — licence reasoning (S14), condition reasoning (S16), and
recognising an unanswerable question (S18). Context relevance of 0.006 is the
`hash` embedder, not the retriever: with no semantic signal the dense half
returns near-random passages. That is the number that should move most on the
real backend.

### Before and after one deliberate change

`RETRIEVAL_TOP_K` 6 → 12 — chosen because it is model-independent and so produces
a real delta even against the stub, and because it has a genuine trade-off:
more chance the governing clause is present, more irrelevant context diluting it,
more tokens.

| metric | before | after | delta |
|---|---:|---:|---|
| groundedness | 0.3421 | 0.3421 | — |
| context relevance | 0.0057 | 0.0059 | +3.5% rel. |
| faithfulness | 1.0000 | 1.0000 | — |
| hallucination rate | 0.0000 | 0.0000 | — |
| total tokens | 285,122 | 288,961 | **+1.3%** |

Read honestly: 1.3% more tokens for a 3.5% relative gain in a metric sitting at
0.006. **That is not a case for shipping it.** The token cost is real and
model-independent; the quality effect cannot be judged from this.

### The prompt comparison did not produce a result

Two complete prompt sets exist with four documented changes, each targeting one
observed failure. Against the stub, every metric comes back **identical** —
because the stub reads only the `[[STEP:...]]` tag and ignores prompt text
entirely. A zero delta here means "nothing read the change", not "the change had
no effect".

Rather than publish the zeros, `prompt-compare` now refuses to run against the
stub and explains why. This is the one requirement nothing in this repository
satisfies, and it is the single most informative measurement left to take: if
change C1 does not move the restricted-use scenarios, the hypothesis behind the
whole design is wrong.

---

## 6. Performance, cost and the bottleneck

### SLO, declared first

**p95 of a complete run, submission to final result, ≤ 45 s.** Written into
`.env.example`, the Prometheus alert and the dashboard threshold before anything
was measured; the justification against section 1 is in
[docs/slo.md](docs/slo.md), a separate file written first.

45 s because this is not a customer-facing wait — the FNOL submission returns in
about 4 ms — it is a recommendation landing in a handler's queue, against an
insurer that commits to appointing a surveyor within 24 hours. It cannot be much
tighter because the work is irreducibly five sequential model calls; it should
not be much looser because then the queue becomes the real limit and nobody
notices.

### Measured

> Outside Docker, `EXECUTION_MODE=inline`, stub at 350 ms × 5 calls.
> [docs/load-test.md](docs/load-test.md) is explicit about what transfers.

| callers | throughput | p50 | p95 | |
|---:|---:|---:|---:|---|
| 20 | 4.2 runs/s | 2.60 s | 2.60 s | met |
| 50 | 9.6 runs/s | 2.60 s | 2.70 s | met |
| 150 | **15.0 runs/s** | 6.30 s | 7.90 s | met |
| 400 | 13.9 runs/s | 19.0 s | 22.0 s | met |
| 600 | 7.9 runs/s | 39.0 s | 41.0 s | met, barely |
| 800 | 9.9 runs/s | 53.0 s | 57.0 s | **not met** |

Submission call itself: p50 3 ms, p95 4 ms. The asynchronous design works — the
front door stays responsive while the back end saturates.

**The target fails at about 640 concurrent callers.** The component responsible
is the **thread pool executing the background tasks**, and the evidence is one
gauge: `triage_inflight_runs` pinned at **38**, never above 40 — Starlette's
default AnyIO worker limit. Little's law agrees from the other side: 40 slots ÷
2.6 s of work = 15.4 runs/s, within 3% of the measured ceiling. The system does
exactly as much work as 40 slots allow.

Not the bottleneck: the stub (single-digit inflight), retrieval (p95 5–25 ms, and
a 99.97% cache hit ratio), the state backend, or validation.

**What does not transfer to containers:** that ceiling and that bottleneck are
artefacts of inline execution. In the compose topology the limit is
`replicas × WORKER_CONCURRENCY / per-run seconds` — at the defaults, about 0.8
runs/s, twenty times lower. That is why `--scale worker=3` exists, and it is the
measurement that is missing.

### Caching (measured)

| | p95 | tokens/run |
|---|---:|---:|
| repeated claim, run cache on | **0.109 s** | **739** |
| repeated claim, run cache off | 2.368 s | 14,771 |
| unique claims, caches on | 2.380 s | 14,031 |
| unique claims, caches off | 2.398 s | 14,770 |

The run cache is worth **95% of latency and 95% of tokens** on repeated traffic
and **nothing** on unique traffic. The embedding and retrieval caches buy ~5% of
tokens and no measurable latency — because both the embedder and the index are
local, so there is little to save. That is a less flattering result than
"caching helps" and it is the one we measured.

### Cost per 100 runs, and the largest driver

**15,390 tokens per run → 1.54 M tokens per 100 runs** (96% input). At free-tier
prices, USD 0.00; priced against a mid-size hosted model, about $0.94 per 100.

| step | tokens | share |
|---|---:|---:|
| `coverage_analysis` | 4,976 | **32.3%** |
| `verification` | 4,484 | 29.1% |
| `query_formulation` | 4,140 | 26.9% |
| `routing` | 936 | 6.1% |
| `intake_classification` | 854 | 5.5% |

The largest driver is the **retrieved context block**: the three expensive steps
are exactly the three that receive it, and it is serialised into the prompt three
times per run. Trimming the verifier's context to the clauses actually cited
would save roughly 25% of the run's tokens. **It was not done** — it touches the
verification path and was not worth the risk late on — and it is the first thing
to do if cost becomes a constraint.

---

## 7. What did not work

### The bug that mattered most: every clause filed under the wrong chapter

The index built cleanly — 357 chunks, no errors, sensible role distribution. But
*Limitations as to Use* did not exist as a heading anywhere, and the General
Exceptions clause was attributed to the third-party liability section.

Two faults compounding. The line-joining heuristic tested whether the character
before a newline was sentence-final; the PDF leaves a **trailing space** there,
so `CHAPTER 4 - Limitations as to Use` was joined onto the paragraph above and
the chunker never saw a heading. Separately, `PRIVATE CAR PACKAGE POLICY WORDING`
appears on all 46 pages, matched the all-caps heading pattern once per page, and
became the dominant heading in the document.

**Nothing failed.** No exception, no warning, and the retrieved *text* was
correct — only its attribution was wrong. A citation would have quoted the right
words and pointed at the wrong chapter and page.

This cost about two hours, most of it spent assuming the problem was embedding
quality because the text looked right. It is why the test suite contains the
exact trailing-space input, and why `strip_running_lines` exists.

### The guardrail was rewriting the text it was protecting

`check_tool_output` neutralised directives in retrieved corpus text and returned
the rewritten version, which the agent then quoted. The verifier checks quotes
against the clause **as stored**, so any quote from a rewritten passage could not
resolve — the run would fail verification, loop all three strategies and
escalate, and the trace would read like a retrieval problem.

It fired constantly, too, because the `tool_coercion` pattern matched
`USE AS A TOOL OF TRADE`, a genuine heading in the wording.

The guardrail now **never** rewrites: it records findings and drops the whole
passage on a high-confidence hit. Dropping is safe — a passage the agent never
sees is one it cannot cite. Altering is not.

### The load test declared the objective met on 100% failures

```
runs 140   failures 140   p95 0.00s   objective 45.00s   VERDICT MET
```

Hard-coded policy dates against a `now()`-derived loss date: every claim failed
`loss_within_policy_period`, every submission returned 422, and because a 422
comes back in 3 ms the run looked *fast*. The ingestion boundary was working
perfectly — that was the problem, it was the only thing being tested.

The summary now refuses to issue a verdict above a 2% failure ratio.

### The cache made its own benefit invisible

Four cache configurations, all four reporting identical tokens per run. A cached
run makes no model calls, so it cannot have spent 14,771 tokens — but
`execute_run` replayed the originating run's `cost`, inflating cost-per-100-runs
by exactly the amount the cache saved. `served_from_cache` and a zeroed cost
fixed it, and the real saving turned out to be 95%.

### The largest cost driver was reported as the cheapest step

`max()` over per-step spend, where every spend is `0.00` on a free tier, returns
whichever key is first — `intake_classification`, which is the cheapest of the
five by a factor of six. Fixed by tracking `tokens_per_step` and falling back to
it. The corrected answer pointed at a real 25% saving; the wrong one pointed at
nothing.

### The common thread

Six of the eight incidents produced a **plausible-looking result rather than an
error.** A heading swallowed into a paragraph, a guardrail quietly rewriting a
quote, a load test submitting nothing valid, a cache inflating its own cost
report — every one would have been read as a success by anyone looking at the
top-line output.

That is the failure mode this kind of system actually has, and it is the whole
argument for the things that look like over-engineering here: the deterministic
citation check that does not trust the verifier, the `valid_for_reporting` flag
on every evaluation report, the load test that refuses to conclude, the
`embeddings: hash` field on the readiness probe. Each one exists because
something looked fine and was not. Full accounts in
[INCIDENT_LOG.md](INCIDENT_LOG.md).

---

## 8. What is not finished

Stated plainly. The brief says completing all 22 requirements is not expected and
that sound prioritisation is assessed, so this is the list rather than a gap to
be found. Per-requirement status:
[docs/requirements.md](docs/requirements.md).

| | |
|---|---|
| **`docker compose up` has never been run.** Docker is not installed on the development machine. Every service, stage, health check and dependency condition is written and reviewed; the entire Python system is verified outside Docker — 167 tests, the full HTTP surface, the agent graph, the circuit breaker, the load test, the evaluation harness. The container build itself is unverified and is the first thing to check. | A7, D3, demo 1 |
| **`--scale worker=3` not measured.** [docs/scaling.md](docs/scaling.md) gives the capacity model, the method, the predictions and a decision procedure for each candidate bottleneck, rather than a fabricated number. | A7, demo 9 |
| **The L2 prompt comparison has not been run against a real model.** The harness is ready; the stub cannot read prompt text. | L2 |
| **Every evaluation number is stub-derived** and flagged as such. | L3 |
| **Load-test numbers were taken in `EXECUTION_MODE=inline`**, so the ceiling and bottleneck are artefacts of that topology. What transfers is stated. | D6 |
| **Retrieval is not orchestrated by LangChain/LlamaIndex.** The chunker had to be hand-written for this corpus; wrapping it would change no logic. Met in substance, not in letter. | L1 |
| **TruLens not used.** Equivalent implemented — four feedback functions, same terms, with a deterministic scorer by default and an LLM judge behind a flag. Reasons: four model calls per record on a free tier, and opacity where a claims system needs explainability. | L3 |
| **Guardrails are rule-based.** Deliberate; limitation stated. | L4 |
| **At-most-once queue delivery.** Known gap, fix named, claim never lost. | A4 |
| **Verifier context not trimmed to cited passages.** A known 25% token saving, left undone deliberately. | L5 |
| **Jaeger export and the Grafana dashboard are unrendered.** Spans are created and panels are written; neither has been seen. | D5, A6 |

### If there were another week

In order:

1. Run `docker compose up`, then `--scale worker=3`, and replace
   [docs/scaling.md](docs/scaling.md)'s predictions with measurements.
2. Point it at a real provider and run `prompt-compare`. If C1 does not move the
   restricted-use scenarios, the central design hypothesis is wrong and that is
   worth knowing more than anything else on this list.
3. Trim the verifier's context to the cited clauses — 25% of tokens, small
   change, measurable with the harness that already exists.
4. Move the queue to Redis Streams with consumer groups.

---

## 9. Assessment summary

| | |
|---|---|
| Services | 10 under Compose, every one with a health check and declared dependencies |
| Agents | 4, with documented hand-offs and an 11-row mid-run failure table |
| Tools | 5, all bounded and traced |
| Contracts | 4, JSON Schema generated from the enforcing models and served live |
| Validation | 4 layers, 14 cross-field rules, quarantine with field-level errors |
| Guardrail surfaces | 3 — input, tool, output |
| Evaluation scenarios | 25, each with its supporting passage |
| Tests | 167, ~4 s, no infrastructure needed |
| Incidents recorded | 8, all genuine |
| Prompt sets | 2, versioned, content-hashed, recorded on every response |
| Corpus | 2 documents, 46 + 8 pages, 381 chunks, 74.6 chunks/s, SHA-256-keyed idempotent ingestion |

**Appendices**

- [README.md](README.md) — how to run it
- [RUNBOOK.md](RUNBOOK.md) — two failure modes in depth, eight shorter ones
- [INCIDENT_LOG.md](INCIDENT_LOG.md) — six real incidents
- [docs/](docs/) — architecture, agent topology, the 4.1 case, contracts,
  retrieval, evaluation, SLO, load test, scaling, cost, stub, testing,
  requirements
