# Bug sweep

A deliberate hunt for defects after the system was feature-complete, prompted
by "I guess there are some bugs". It used three instruments, two of them new:

| instrument | what it drives | what it found that nothing else did |
|---|---|---|
| `pytest` | the code in-process, synthetic corpus | — (it passed throughout; that was the point) |
| `scripts/sweep.py` | the live API, 47 cases in five groups: valid, benign-but-alarming, attacks, malformed, edge | validation, guardrail and model-shape defects |
| `scripts/ui_e2e.py` | the web page in real Chrome, every scenario card, plus the outage switch, with screenshots | the import deadlock, and every defect in what the result *says* |

The unit suite was green before and during all of this. Nearly every defect
below produced a plausible answer rather than an error, which is why it took
running the real thing and reading the output to find them.

```bash
bash scripts/run-local.sh                                   # terminal 1
python scripts/sweep.py  --api http://127.0.0.1:8080        # 47 live cases
python scripts/ui_e2e.py --api http://127.0.0.1:8080 --shots shots/   # 11 browser checks
python scripts/ui_e2e.py ... --theme dark | --width 420 | --only food
```

Result at the end: **227 tests, 47/47 sweep cases, 11/11 browser checks**.

## What was found

### The evidence was wrong: the section 4.1 case cited the wrong grant

The headline case, a food-delivery claim, reached the right position
(likely not covered, investigate) but its GRANT card was a prospectus line
about paying the premium in EMI instalments. Five separate defects stacked up
to cause that. Each is fixed and each has a test. See INC-009.

| defect | fix |
|---|---|
| "Page 3 of 8" footers glued onto chunks, and one was cited as a grant | page-number lines stripped on every page of every document, short ones included |
| the own-damage grant shredded: each roman item (i., ii., iii.) became its own section, each nested under the one before | heading stack pops siblings; items stay inside the clause that introduces them |
| "means" alone tagged "by accidental external means", the heart of the grant, as a *definition* | a definition must say a term "means the/any/a…" or "shall mean" |
| fused ranking depended on `top_k`: candidate pools were `top_k × 3` deep, so the grant ranked 3rd at k=8 and vanished at k=6 | fixed-depth candidate pools (50); the hash embedder's ranking is down-weighted to 0.25 in fusion because it carries no meaning |
| named-clause lookups (scores 1.0 and 0.7) filled every context slot and pushed the grant out | each role gets a floor of context slots before the rest is filled by score |

### The result page contradicted itself

Found by reading the browser screenshots, not by any assertion. See INC-011.

| what the page said | cause | fix |
|---|---|---|
| "Low value, drivable vehicle…" over a ₹2.6 lakh undrivable car, then "Raised to surveyor_assignment by mandatory rules: vehicle_undrivable, high_value" | the overruled model's reason was printed first, as fact | a rules override now leads: "Raised from fast_track to surveyor_assignment by mandatory rules: …", followed by "The routing model had proposed fast_track: '…'". The stub no longer asserts facts it does not read |
| an EXCLUSION badge on "This Policy and the Schedule shall be read together" | roles matched against the whole heading path, so a CONDITIONS sub-section inherited "exclusion" from its chapter title, "Exception, Condition & Deductible" | nearest evidence first: own heading, then text, then ancestors. Six clauses re-tagged |
| every quote was the clause's first 18 words, whatever its role | stub quoting | the stub quotes the sentence that earned the role |
| "BUT — takes cover away" under a LIKELY COVERED stamp | the stub's covered case cited an exclusion | a covered position cites the grant and its conditions |
| the grant quote began "1. The Company…" | follows from quoting the first words | the same fix |

### Escalation and freshness: safety nets that could not fire

| defect | effect | fix |
|---|---|---|
| hit scores were `rrf / best_rrf`, so the top hit was 1.0 for every query, gibberish included | the weak-retrieval escalation (`best < threshold`) was dead code | scores are absolute evidence: max(cosine, saturated BM25) |
| a long-lived index never noticed a rebuild done by another process | the API served the old corpus until restarted | the index checks `index_version` every 5 s and reloads |
| a citation's role badge came from the model | a model could badge an exclusion "grant" | roles come from the index |

### The model's output shape

| defect | fix |
|---|---|
| `"queries": "limitations as to use…"` (a string, not a list) sliced with `[:2]` searched for "l" and "i" | `agents/coerce.py` normalises every model field at the five parse sites |
| the stub labelled a food-delivery kerb strike a theft: it scanned the structured summary, which always contains the key `"vehicle_stolen"` | it reads only the customer's text and the actual flag value |

### Guardrails

| defect | fix |
|---|---|
| ordinary claim text flagged: "act as the assessor", "the surveyor shall approve…" | two tiers: strong patterns block alone; medium ones only flag, and block only in combination (combined score ≥ 0.90) |
| "I am going to kill myself, this car was everything" **blocked as a threat of violence** | self-harm is not a threat to anyone else: a welfare flag, urgent priority, first touch within 1 h, and the routing reason opens with "Contact the customer first" |

### Validation

| defect | fix |
|---|---|
| Bharat-series plates (`22BH1234AB`) rejected | BH pattern accepted alongside the state format |
| phone numbers with dashes, spaces or brackets rejected | separators stripped before the check |

### The service

| defect | found by | fix |
|---|---|---|
| "service unreachable" on first page load | the browser test (see INC-010) | imports moved to module level; the index warms at start-up; the page retries readiness |
| a cache hit reported the original run's steps and cost as if they had been spent again | sweep | `served_from_cache`, `cached_from_run_id`, cost zeroed, steps empty |
| no way to watch a run, force a fresh run, or tell which model is live | sweep | `live_steps` on run status, an `X-Bypass-Cache` header, and a `model` block in readiness |

## The interface

The page went through three designs in this pass. The current one is a
claims-officer workspace, redesigned because the second version showed
everything at once: a permanent inbox column, every policy clause, every metric.

The principle is *show what needs a person; keep the rest one click away.*

- **Needs your attention.** Built from real fields only: `routing.priority`,
  `routing.target_first_touch_hours` and `completed_at` (so the SLA deadline is
  triage completion plus the target, never an invented one), and the
  hand-to-a-human queue. Ordered overdue, urgent, handed-over, high,
  approaching. A claim the officer has decided stops appearing.
- **A Claims drawer** replaces the permanent inbox: search, five filters, and a
  compact row per claim. The statuses are derived from what the service
  returned and what the officer decided, not stored separately.
- **Progressive disclosure** in the evaluation panel: recommendation,
  verification, key question and action card are always visible. The policy
  evidence, the activity log and the technical trace are collapsed. The two
  decision buttons and a one-line metrics footer are pinned to the bottom of the
  window.
- A blocked claim shows the findings as one card naming each matched pattern,
  with the description highlighted at exactly the matched phrases.
- No web fonts and no external requests. Light and dark themes. On a phone the
  drawer is a full-height sheet and the evaluation sits under the claim.

Three defects were found by looking at the result rather than by any assertion:
the right panel spilled 48px past its card when the evidence was open, the
phone layout scrolled sideways (both were grid tracks sized `1fr` where
`minmax(0, 1fr)` was needed, because one wide child sets the track's width), and
the action card sat under the pinned buttons on a laptop-height window. The
browser test now asserts the first two at every width.

## What this sweep did not do

- It ran against the stub model only. The stub cannot read a clause, so
  "the reasoning is right" is still unverified. What *is* verified is that the
  right clauses are retrieved, cited, quoted and badged.
- The evaluation-set numbers in [evaluation.md](evaluation.md) and the
  load-test numbers were measured on the 381-chunk index from before the
  chunker fixes. The corpus is now 309 chunks. Re-run both before quoting them
  against this version.
