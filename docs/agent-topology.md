# Agent topology (A1, A2, A3)

Four agents, one shared state object, explicit hand-offs, three independent loop
bounds. Implemented in `claimtriage/agents/graph.py` with LangGraph.

```
                             START
                               │
                    ┌──────────▼───────────┐
                    │   INTAKE ANALYST     │
                    │   intake_classification
                    └──────────┬───────────┘
          hand-off: {claim_type, confidence, entities, retrieval_queries}
                               │
   ┌───────────────────────────▼─────────────────────────────────┐
   │                   COVERAGE ANALYST                          │
   │                                                              │
   │   ground_search ──▶ plan_modifiers ──┬──▶ targeted_search    │
   │                     (decides whether │         │             │
   │                      to retrieve)    └──────▶  │   (skip)    │
   │                                                ▼             │
   │                                            analyse           │
   └───────────────────────────┬─────────────────────────────────┘
          hand-off: {draft determination, citations, passages seen}
                               │
                    ┌──────────▼───────────┐
                    │      VERIFIER        │◀────────┐
                    │  1. quotes resolve?  │         │
                    │  2. claims entailed? │         │
                    └──┬────────┬──────────┘         │
            verified   │        │  unsupported       │ replan
                       │        │  + budget left     │ (new strategy)
                       │        └────────────────────┘
                       │        │  unsupported, no budget
                       │        │  or clauses conflict
                       │        ▼
                       │   ┌─────────────┐
                       │   │  ESCALATE   │──▶ END
                       │   └─────────────┘
            ┌──────────▼───────────┐
            │   ROUTING OFFICER    │──▶ END
            │  model choice, then  │
            │  floored by the rules│
            └──────────────────────┘
```

## Why these are four agents and not four prompts

A2 says two prompts in one process are not two agents. The distinction we are
holding to is that each of these owns a part of the state, can fail
independently, and has a defined interface to the next one. Specifically:

| agent | owns | may not |
|---|---|---|
| **Intake Analyst** | `classification` — claim type, entities, the discrepancy between declared use and description, the first retrieval queries | touch coverage, routing or the corpus |
| **Coverage Analyst** | `plan`, `draft`, and all tool use | route, or decide whether its own answer is adequate |
| **Verifier** | `verification`, and the only authority to send work back | write a determination, or change one |
| **Routing Officer** | `routing` | revisit the coverage position |

The Verifier is the one that makes this a topology rather than a pipeline. It is
**adversarial to the Coverage Analyst by construction**: it is given the
passages and the draft, *not* the Analyst's reasoning, and it is the only node
that can loop. `prompts/v2/verification.md` opens by telling it so —

> You are a different reader from the one who wrote the answer, and you are not
> trying to be helpful to them.

and it is the only agent whose output can override another's.

The Routing Officer is separate for a different reason: its decision is partly
not the model's. The deterministic signals in `claimtriage/agents/signals.py`
compute a **floor** from the validated structured fields, and
`signals.enforce()` raises the model's choice to that floor, recording the
override in `routing.overridden_by_rules`. A model is allowed to be more careful
than the rules, never less. A model that wants to fast-track a claim with a
bodily injury is overruled and the reason is visible in the result.

## Retrieval is a tool the agent chooses (A1)

Two nodes decide whether retrieval happens at all and what to retrieve:

* `intake_classification` emits `retrieval_queries` — the clauses it would look
  up first. `ground_search` runs those, not a fixed query.
* `plan_modifiers` answers whether any provision could displace the grant, and
  names what to fetch. **If it answers no, no second retrieval happens** —
  `_after_plan` routes straight to `analyse`. That branch is what makes this a
  tool the agent calls rather than a stage it passes through.

### The five tools

| tool | what it does | why it exists |
|---|---|---|
| `corpus_search(query, role, top_k, strategy)` | hybrid dense + BM25 search, optionally filtered to one clause role | the general case |
| `find_named_clause(name)` | resolves a clause the wording refers to **by name** | the section 4.1 case. The general exception names the Limitations as to Use; this is how the agent goes and reads it |
| `get_clause(clause_id)` | one clause by identifier | citation resolution, and following a reference |
| `clause_neighbours(clause_id, window)` | the clauses either side, in document order | policy wordings put the proviso in the next sentence and the carve-out in the next sub-clause |
| `get_claim_field(path)` | a validated field of the FNOL by dotted path | the agent should not re-derive `vehicle_use_at_time_of_loss` from prose when the contract carries it as an enum |

Every call is bounded by `TOOL_TIMEOUT_SECONDS`, counted against the run's tool
budget, timed, traced, and screened by the tool-surface guardrail before its text
reaches a prompt.

## Retrying with a different strategy (A3)

Retrying a failed search the same way is how a system convinces itself. The
ladder is `SEARCH_STRATEGIES` in `graph.py`, and the `replan` node advances it:

| attempt | strategy | what it does differently |
|---|---|---|
| 1 | `analyst_queries` | resolve the clauses the Analyst named, run the queries it wrote |
| 2 | `restriction_sweep` | read the declared use from the contract, then sweep the corpus **filtered to `restriction` and `exclusion`**, and resolve all three named restriction clauses by name |
| 3 | `neighbour_and_lexical` | expand around the clauses already cited, plus a **lexical-only** pass over the policy's terms of art |

Each looks for a different kind of thing. Strategy 2 asks the corpus a question
the claim text cannot express; strategy 3 abandons semantics entirely and matches
the exact phrases. `tests/test_agent.py::test_retry_uses_a_different_strategy_each_time`
asserts no strategy is used twice in one run.

## Verification (A3)

Two checks, cheap one first, in `_node_verify`:

**1. Deterministic citation resolution.** For every citation, is the quote found
character-for-character (whitespace-normalised) in the clause it is attributed
to? No model is needed to answer that and no model should be trusted to. A
citation that does not resolve is a fabrication regardless of what any verifier
says about it.

**2. Model entailment.** Per-claim, against the passages that were available.
`supported`, `unsupported` or `contradicted`, with the specific statements that
failed and any pair of clauses in conflict.

The verdict is `verified` only when the model says verified **and** no citation
is unresolved **and** the combined score (half model support, half citation
resolution rate) is at or above `VERIFIER_MIN_SUPPORT`.

Then, in `runner._assemble`, **only resolving citations are carried into the
result.** "Every citation must resolve to retrieved text" is therefore a property
of the response object, not an instruction in a prompt —
`tests/test_agent.py::test_every_returned_citation_resolves` checks it, and
`test_completed_runs_are_always_verified` checks that an unverified answer never
comes back as `completed`.

## Loop bounds (A1)

Three, all from configuration, all independent:

| bound | default | checked |
|---|---|---|
| steps | `AGENT_MAX_STEPS=14` | `_budget_exhausted` before each retry decision |
| tool calls | `AGENT_MAX_TOOL_CALLS=12` | in `tools._invoke`, before every call |
| wall clock | `AGENT_WALL_CLOCK_SECONDS=120` | in `_invoke`, and it also caps each tool's own timeout |

LangGraph's `recursion_limit` is set to `3 × AGENT_MAX_STEPS` as a backstop
against a cycle our own accounting misses.

A run that hits a bound **does not return a confident answer.** It escalates with
`loop_budget_exhausted` and the explanation names the bound that stopped it.

## What happens when an agent fails mid-run

A2 asks for this to be documented. Each failure has a defined path, and all of
them end in a result object rather than an exception.

| failure | path |
|---|---|
| **Intake call fails** (parse error, provider error) | fall back to `_classify_without_model`, which derives the claim type from the validated structured fields. `degraded=true`, path `intake_classified_from_structured_fields_only`. The run continues. |
| **Intake hits an open circuit** | escalate `provider_unavailable`. No point starting work that cannot finish. |
| **Ground search returns nothing, or nothing above `ESCALATION_MIN_RETRIEVAL_SCORE`** | escalate `weak_retrieval`. This is the out-of-corpus path (F7). |
| **Planner call fails** | fall back to a fixed restriction sweep — Limitations as to Use and General Exceptions by name. Strictly better than skipping the modifier search, which is the section 4.1 failure. `degraded=true`. |
| **Tool budget or wall clock exhausted during targeted search** | **not** fatal. Carry on to analysis with what is held and let the Verifier decide. `degraded=true`. |
| **Analysis call fails or returns unparseable output** | escalate. There is no fallback here on purpose: a coverage position is the one thing that must not be improvised. |
| **Analysis hits an open circuit** | escalate `provider_unavailable`, with the retrieved passages attached for the handler. |
| **Verifier call fails** | fall back to the deterministic half alone, and require a **perfect** citation resolution rate before accepting the answer — the deterministic check is stricter about citations and blind to reasoning, so the bar goes up when it is working alone. |
| **Routing call fails** | route on the deterministic signals alone. A complete, defensible decision; simply more cautious than the model would have been. |
| **The graph itself raises** | `runner.execute_run` catches it, returns `status=failed`, `queue=human_escalation`, and the error. A claim never falls out of the pipeline with no queue. |
| **The worker process dies mid-run** | the run stays `running` in the store. `restart: unless-stopped` brings the worker back; the job is gone from the queue (Redis `BLPOP` removes it on read), so the run needs resubmitting. This is the known gap — see below. |

### The known gap: at-most-once delivery

`BLPOP` removes a job from the list the moment a worker reads it. If that worker
dies before writing the result, the job is lost and the run sits at `running`
until its TTL expires.

This is a deliberate choice for this stage, not an oversight. The alternative —
Redis Streams with consumer groups and explicit acknowledgement — gives
at-least-once delivery and the ability to reclaim an abandoned job, at the cost
of a reclaim loop, a visibility timeout to tune, and idempotency handling for
runs that get executed twice. Executing a triage run twice is harmless (it is
advisory and read-only), so at-least-once would be safe here, and it is the right
next change. What made it wrong *now* is that the claim is never lost — it is in
the run store, visible in `GET /v1/runs`, and a stuck run is detectable. The
runbook covers finding and resubmitting one.

## Observability of the topology

Per-agent and per-step metrics (A6), all labelled by `agent` and `step`:

- `triage_agent_task_total{agent, result}` → task success rate per agent
- `triage_agent_step_duration_seconds{agent, step}` → per-step latency
- `triage_run_steps`, `triage_run_tool_calls` → steps and tool calls per run
- `triage_run_duration_seconds` → end-to-end latency
- `triage_llm_retries_total` → retry rate
- `triage_service_start_timestamp_seconds{role}` → container uptime
- `triage_verification_total{outcome}` → verified / unsupported / retry / escalate

And every step is written into the result as a `StepRecord` carrying its agent,
step, outcome, duration, prompt version, token counts, cost, the tool it called,
the query it issued, the clauses that came back and the top score — which is what
makes `GET /v1/runs/{id}/trace` a full reconstruction rather than a summary.
