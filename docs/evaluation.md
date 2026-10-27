# Evaluation (L2, L3)

> **Every number in this file was measured against the stub provider and is
> marked `valid_for_reporting: false` in the report it came from.** The stub
> cannot read a policy clause. These numbers demonstrate that the harness, the
> metrics, the grading and the before/after machinery work; they are *not* a
> measurement of answer quality. What to re-run with a real provider, and what
> to expect of it, is at the end of each section.

## The evaluation set

25 scenarios in `eval/dataset.yaml`, each with its expected outcome and the
passage in the corpus that supports it. Runnable from the command line:

```bash
docker compose run --rm tools eval --out /app/eval/reports/latest.json
docker compose run --rm tools eval --only S01_food_delivery_reference_case
```

Scenarios are written as a patch over a shared `base` FNOL, deep-merged by the
harness, so what each one is testing is visible without reading sixty lines of
boilerplate.

| category | n | what it tests |
|---|---|---|
| `restricted_use` | 5 | the section 4.1 mechanism: S01 (reference), S10 ride-hailing, S11 carriage of goods, S12 speed testing, S13 driving tuition |
| `restricted_use_control` | 1 | S02 — commuting is *also* a purpose and must **not** be treated as restricted |
| `ordinary` | 3 | the short path: minor own damage, windscreen, undrivable |
| `third_party` | 2 | property damage, bodily injury with an FIR |
| `theft` / `total_loss` | 2 | mandatory investigation and surveyor floors |
| `driver_clause` | 2 | no valid licence, learner licence only |
| `conditions` / `procedure` | 2 | late notification, claim procedure |
| `out_of_corpus` | 2 | **must escalate rather than answer** (F7) |
| `guardrail` | 2 | prompt injection, threat — must be rejected at the boundary |
| `validation` | 4 | must never start a run |

Three kinds of expected outcome: `completed` (should produce a position),
`escalated` (should decline), `rejected` (should never start a run).

### Grading

Per-scenario expectations are declarative — position, queue, minimum citations,
a section path or phrase that must appear, a validation warning that must fire,
an acceptable escalation reason. Each is optional, so a scenario asserts only
what it is about.

On top of those, two **unconditional invariants** are checked on every run in
every scenario:

1. No citation may be returned that does not resolve to retrieved text.
2. A run returned as `completed` must have a `verified` determination.

Those two are the properties that make the whole system trustworthy, so they are
not left to individual scenarios to remember.

## The four metrics

L3 requires groundedness, context relevance, faithfulness and hallucination
rate, and requires stating how each is computed. All four are in
`claimtriage/eval/metrics.py` with the thresholds in the module docstring.

### groundedness

Of the atomic statements in the determination — `summary` plus `reasoning`,
split on sentence boundaries, fragments under 20 characters dropped — the share
for which *some* retrieved passage supports the statement.

Support is content-word recall of the statement against the best-matching
passage, at or above **0.45**. Content words exclude stopwords and six
domain-filler words (*policy, claim, insured, company, vehicle, shall*) which
appear in nearly every passage and would otherwise inflate every score.

The threshold was set by labelling 40 statements by hand and choosing the point
that separated them best. It is a knob and it is written down.

### context relevance

Of the passages put in front of the model, the share that bear on the question.
Content-word overlap between the passage and the claim description plus the
expected-clause hints, at or above **0.12**.

That threshold is low on purpose. A policy clause and a claim description share
very little vocabulary *by nature* — that is the entire difficulty of this
corpus — so this metric detects "entirely unrelated", not "closely on point". A
threshold tuned to mean the latter would score the correct answer to the section
4.1 case as irrelevant context, because the limitations clause shares almost no
words with the claim.

### faithfulness

Of the citations attached to the answer, the share whose quote is found
character-for-character (whitespace-normalised) in the clause it is attributed
to **and** whose clause was actually retrieved during the run.

This is the strictest of the four and the one that cannot be argued with: a
quote either appears in the source or it does not. It is also the metric the
system enforces at runtime — unresolved citations are dropped before the result
is assembled — so a faithfulness below 1.0 in a report indicates a run where the
verifier accepted something it should not have.

### hallucination rate

The share of records where the system asserted something it could not support: a
**confident** position — `completed`, not `undetermined`, no escalation — that
had either no resolving citation at all, or at least one citation that failed to
resolve.

**Escalating is explicitly not a hallucination.** Nor is `undetermined`.
Declining to answer is the required behaviour under F7, and a metric that
punished it would make the cheapest route to a good score "answer everything
confidently" — the single worst outcome this system can produce. There is a
separate signal for the opposite failure: `EscalationRateHigh` fires when more
than half of runs escalate.

`tests/test_eval.py::test_escalating_is_not_a_hallucination` pins this.

### Two scorers per metric

| scorer | cost | used |
|---|---|---|
| lexical | free, deterministic, runs anywhere | every record, every run |
| LLM-as-judge | one model call per record | only with `--judge`, and only when a real provider is configured |

Both are reported when the judge is enabled, so the deterministic numbers stay
comparable across runs and the judged numbers are there when a sharper read is
wanted.

The lexical scorer has a known, stated limitation: no stemming, so *used* and
*use* are different tokens. The effect is that lexical groundedness is a slight
**under**-estimate — the direction we would choose if we had to choose, since it
will not flatter the system. `test_recall_is_high_but_not_perfect_across_inflections`
documents it.

### Why not TruLens

L3 says "TruLens or equivalent". We started with TruLens and moved off it for two
reasons.

Its feedback functions are themselves model calls — four per record. On a free
tier with a low rate limit, evaluating 25 scenarios costs about a hundred extra
calls and takes longer than the runs being scored. Given that the cost section
of the brief specifically warns that agents multiply model calls, spending 4× the
run budget on scoring was the wrong trade.

And the feedback functions are opaque at exactly the point where a claims system
needs them not to be. When a coverage position is judged ungrounded we have to be
able to say *which sentence* and *which passage* — `score_record` returns
`ungrounded_statements` for precisely that. "The classifier said 0.71" is not
something a compliance reviewer can act on.

So the same four feedback functions are implemented, in the same terms, with the
deterministic scorer as the default and the LLM judge available behind a flag.

## Results: the evaluation set

`eval/reports/smoke_stub.json`, prompt set v2, stub provider, hash embeddings.

```
scenarios 25   passed 22   failed 3   pass rate 88.0%
escalated 0    rejected at boundary 6    agent runs 19

groundedness           0.3421
context_relevance      0.0057
faithfulness           1.0000
hallucination_rate     0.0000

mean steps 7.0   mean tool calls 5.32   mean duration 2.14s
total tokens 285,122
```

**What this does and does not show.**

It shows: the boundary rejects all six scenarios that must be rejected, with no
agent run started in any of them; the graph executes 7 steps and 5.3 tool calls
per run; every citation that comes back resolves (faithfulness 1.0); nothing is
asserted without support (hallucination 0.0); the section 4.1 reference case
passes its position, queue and citation assertions.

It does not show anything about reasoning quality. The three failures are all
things the stub cannot do:

| scenario | why it fails against the stub |
|---|---|
| `S14_driver_without_valid_licence` | the stub has no licence logic; it keys only on declared use |
| `S16_late_notification` | the stub's prose never mentions notice or conditions |
| `S18_out_of_corpus_cyber` | the stub always answers; it cannot recognise an unanswerable question |

`context_relevance` of 0.006 is the `hash` embedder, not the retriever: with no
semantic signal the dense half returns near-random passages. On the real backend
this is the number that should move most.

## L2: comparing two prompt sets

Two complete sets exist, `prompts/v1` and `prompts/v2`, selected by `PROMPT_SET`
— configuration, not a code change, which is what makes the comparison
reproducible. `prompts/registry.yaml` records the owner, status and the four
specific changes with the failure each one targets:

| id | step | change | targets |
|---|---|---|---|
| C1 | `query_formulation` | names the four ways a grant can be displaced in this corpus and requires the model to state, for each, whether it has checked. v1 asked for "any other relevant clauses", which the model answered by restating the clause it already had. | **the section 4.1 failure** — the modifying provision is never searched for |
| C2 | `coverage_analysis` | requires a verbatim quote per citation, ≤40 words, paraphrase forbidden. v1 allowed a summary, making citation resolution unverifiable. | groundedness, citation resolution |
| C3 | `coverage_analysis` | adds an explicit `undetermined` position with the instruction to use it rather than guess, and requires non-empty `open_questions` below 0.6 confidence. | hallucination rate on out-of-corpus questions (F7) |
| C4 | `verification` | changes the verifier from "is this reasonable" to a per-claim entailment check returning the specific statements that failed. | faithfulness, and making A3 enforceable rather than advisory |

```bash
docker compose run --rm tools prompt-compare --sets v1,v2
```

### Result against the stub: no result

```
  groundedness           0.3421 -> 0.3421  (+0.0000)
  context_relevance      0.0057 -> 0.0057  (+0.0000)
  faithfulness           1.0000 -> 1.0000  (+0.0000)
  hallucination_rate     0.0000 -> 0.0000  (+0.0000)
  pass_rate              0.8800 -> 0.8800  (+0.0000)
```

Identical on every metric — because **the stub does not read prompt text.** It
reads the `[[STEP:...]]` tag and the clause ids in the context block and ignores
everything else. A zero delta here means "nothing read the change", not "the
change had no effect", and those are very different claims.

Rather than publish the zeros, `prompt-compare` now refuses to run against the
stub without `--allow-stub` and prints why. This is the one requirement in L2
that is **not satisfied** by anything in this repository: the comparison needs a
real provider, the harness is ready for it, and the number is not yet taken.

What to expect when it is run: C1 should move the section 4.1 family (S01, S10,
S11, S12, S13) and nothing else; C2 and C4 should move faithfulness; C3 should
move the hallucination rate on S18 and S19. If C1 does not move the
restricted-use scenarios, the hypothesis behind the whole design is wrong, and
that is the single most informative measurement left to take.

## L3: one deliberate change, before and after

The change: **`RETRIEVAL_TOP_K` 6 → 12** — show the analyst twice as many
passages.

It was chosen because it is model-independent, so it produces a real before/after
even against the stub, and because it has a genuine trade-off worth measuring
rather than an obvious right answer: more passages means more chance the
governing clause is present, and more irrelevant context diluting it, and more
tokens.

```bash
RETRIEVAL_TOP_K=6  docker compose run --rm tools eval --out /app/eval/reports/topk_6.json
RETRIEVAL_TOP_K=12 docker compose run --rm tools eval --out /app/eval/reports/topk_12.json
docker compose run --rm tools compare /app/eval/reports/topk_6.json /app/eval/reports/topk_12.json
```

| metric | before (k=6) | after (k=12) | delta | |
|---|---|---|---|---|
| groundedness | 0.3421 | 0.3421 | 0.0000 | flat |
| context relevance | 0.0057 | 0.0059 | +0.0002 (+3.5%) | marginally better |
| faithfulness | 1.0000 | 1.0000 | 0.0000 | flat |
| hallucination rate | 0.0000 | 0.0000 | 0.0000 | flat |
| pass rate | 0.880 | 0.880 | 0.000 | flat |
| total tokens | 285,122 | 288,961 | +3,839 (+1.3%) | worse |

**Reading this honestly:** against the stub the change costs 1.3% more tokens and
buys a 3.5% relative improvement in a metric sitting at 0.006. That is not a
case for shipping it. The *reason* the effect is so small is that the stub cites
whichever clause ids appear first in its context regardless of what they say, so
doubling the candidates changes almost nothing downstream — which is exactly the
limitation this whole file is about.

The measurement is still worth having, for two reasons. The before/after
machinery is demonstrated working, including the direction-awareness that matters
(`compare_reports` knows hallucination rate is better when lower —
`test_compare_reports_reads_the_direction_of_each_metric_correctly`). And the
token cost of the change is real and model-independent: 1.3% is what it costs
whatever model is behind it.

With a real provider the interesting question is whether k=12 raises
groundedness enough to pay for the tokens, and whether the extra context
*lowers* it by dilution — a well-documented failure mode and the reason k=6 is
the default.

## Automatic scoring of live runs (L6)

One in five completed runs is scored automatically against the same four metrics
and recorded with a timestamp (`QUALITY_SAMPLE_RATE` in
`claimtriage/worker/executor.py`). The retrieved passages are reconstructed from
the step records, which is the only evidence a finished run leaves behind — and
another reason the trace records the clause ids each tool call returned.

The rolling values are exposed as `triage_live_quality_score{metric}` and charted
on the dashboard, and the series is available alongside the caller feedback at
`GET /v1/feedback/summary`:

```json
{
  "total": 12, "satisfactory": 9, "satisfaction_rate": 0.75,
  "by_queue": { "investigation": { "satisfactory": 4, "unsatisfactory": 1 } },
  "corrections": [ { "run_id": "...", "observed_queue": "fast_track",
                     "corrected_queue": "investigation", "reason": "..." } ],
  "automatic_quality": {
    "sampled_runs": 8,
    "rolling_metrics": { "groundedness": 0.71, "faithfulness": 1.0, ... },
    "series": [ ... ]
  }
}
```

One in five rather than all of them keeps the cost of the extra scoring pass
proportionate while still producing a usable series within an hour of normal
traffic. The `corrections` list is the most valuable output: a handler
disagreeing with the queue is the clearest training signal this system can get,
and it is the input to the next prompt revision.
