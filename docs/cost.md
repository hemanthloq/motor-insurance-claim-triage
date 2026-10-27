# Cost (L5)

Prompt tokens, completion tokens and spend are recorded at the time of the call
and attributed to the step that made it. Both per step and per run.

## Where the tokens go

One section 4.1 run, prompt set v2, `RETRIEVAL_TOP_K=6`, stub provider:

| step | tokens | share | prompt | completion |
|---|---:|---:|---:|---:|
| `coverage_analysis` | 4,976 | **32.3%** | 4,615 | 361 |
| `verification` | 4,484 | **29.1%** | 4,446 | 38 |
| `query_formulation` | 4,140 | **26.9%** | 4,054 | 86 |
| `routing` | 936 | 6.1% | 885 | 51 |
| `intake_classification` | 854 | 5.5% | 753 | 101 |
| **total** | **15,390** | | 14,753 | 637 |

**The largest single cost driver is `coverage_analysis`, and the underlying
cause is the retrieved context block.** The three expensive steps are exactly the
three that receive it, and together they are 88% of the run. Completion tokens
are 4% of the total; this is an almost purely input-token workload.

That is the useful finding, and it points at an obvious optimisation that has
not been made: the context block is serialised into the prompt **three times**
per run. The verifier in particular is shown all twelve retrieved passages when
it only needs the ones that were actually cited. Trimming the verifier's context
to the cited clauses would cut roughly 25% of the run's tokens at no obvious
cost to the check it performs — it is the first thing to do if cost becomes a
constraint, and it is noted in [REPORT.md](../REPORT.md) as not done.

### A bug this measurement found

The first version of this table reported `intake_classification` as the largest
cost driver, which is wrong by a factor of six. On a free tier every per-step
*spend* figure is `0.00`, and `max()` over a dict of equal values returns
whichever key happens to be first.

`CostBreakdown` now carries `tokens_per_step` alongside `per_step` spend, and
`largest_driver_basis` says which one the answer came from:

```json
{ "largest_driver": "coverage_analysis", "largest_driver_basis": "tokens" }
```

Spend is used when any price is configured; tokens otherwise. Tokens are the
invariant unit — they identify the same step whatever the price list says, and
they are what a rate limit is denominated in. Recorded as INC-005.

## Cost per 100 runs

```
15,390 tokens/run  ×  100  =  1.54 M tokens per 100 runs
                              (1.48 M prompt, 0.06 M completion)
```

At `LLM_PRICE_PROMPT_PER_MTOK=0` and
`LLM_PRICE_COMPLETION_PER_MTOK=0` — a free tier — that is **USD 0.00 per 100
runs**, which is the configuration this project runs in and is why the brief says
all software and data required are free.

The token figure is the one that matters, because it is what a free tier actually
rations. For scale, priced against three published rate cards at the time of
writing (set `LLM_PRICE_*_PER_MTOK` in `.env` to get these computed for real):

| | prompt $/Mtok | completion $/Mtok | per 100 runs |
|---|---:|---:|---:|
| a small hosted model | 0.10 | 0.30 | ~$0.17 |
| a mid-size hosted model | 0.60 | 0.80 | ~$0.94 |
| a large frontier model | 3.00 | 15.00 | ~$5.40 |

So a 2,000-claim month is somewhere between $3 and $108 depending on the model —
small in absolute terms, and entirely dominated by input tokens, which is what
makes the context-deduplication optimisation above the right lever rather than
switching to a cheaper model.

Live, on the dashboard:

```promql
triage:cost_per_100_runs_usd    # 100 * rate(cost) / rate(runs), over 30m
sum by (step) (increase(triage_llm_cost_usd_total[$__range]))   # the pie chart
sum by (step) (rate(triage_llm_tokens_total[2m]))               # tokens/s by step
```

## What the caching does to cost

From [load-test.md](load-test.md), 20 runs per cell:

| | tokens per run | vs no cache |
|---|---:|---:|
| repeated claim, run cache on | **739** | **−95%** |
| repeated claim, run cache off | 14,771 | — |
| unique claims, all caches on | 14,031 | −5% |
| unique claims, all caches off | 14,770 | — |

The run cache is the only one that moves cost materially, and only on repeated
traffic — one of twenty runs paid and nineteen were free. The embedding and
retrieval caches save about 5% of tokens, which comes from the agent's second
retrieval pass re-issuing queries the first pass already made.

A cached run reports **zero** cost, as it should: it makes no model calls. Getting
that wrong is INC-004 in the incident log — the earlier version replayed the
originating run's token count, inflating the cost report by exactly the amount
the cache saved.

## Why cost is bounded at all

The cost section of the brief is specific: agents multiply model calls, free
tiers have low rate limits, and one client looping against a multi-step agent can
exhaust the budget in minutes. Four things in this system exist for that, and
they are all configurable:

| control | default | what it bounds |
|---|---|---|
| `AGENT_MAX_TOOL_CALLS` | 12 | retrieval volume, and therefore context size |
| `AGENT_MAX_STEPS` | 14 | how many model calls one claim can ever make |
| `AGENT_WALL_CLOCK_SECONDS` | 120 | a run that is going nowhere |
| `RATE_LIMIT_REQUESTS` / `_WINDOW_SECONDS` | 30 / 60 | **per client.** This is a budget control first and a fairness mechanism second |

Worst case for a single claim: the verification loop runs all three strategies,
so 3 × (`plan` + `analyse` + `verify`) + `intake` + `routing` = 11 model calls,
against 5 on the happy path. The observed mean across the evaluation set is
**7.0 steps and 5.3 tool calls**, so the loop is the exception rather than the
norm — but the bound is what guarantees it.

`triage_llm_tokens_total{step,kind}` and `triage_llm_cost_usd_total{step}` are
counters, so the dashboard shows spend by step over any window and the pie chart
answers "what is the largest driver" without anyone having to reason about it.
