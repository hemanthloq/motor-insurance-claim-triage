# Presentation deck

**[claim-triage-deck.pptx](claim-triage-deck.pptx)** — 15 slides, 16:9, about
12 minutes of talking at 1.5–2 minutes per slide. Speaker notes are on every
slide.

## Running order

| # | slide | the one thing it has to land |
|---:|---|---|
| 1 | Title | Advisory only — a design constraint, not a disclaimer |
| 2 | What a claims handler actually does | A system that always answers is easy; one that knows when not to is not |
| 3 | **One sentence, three clauses** | The clause that decides the outcome is the one the question cannot reach |
| 4 | Why retrieve-then-answer gets this wrong | Structural, not a tuning problem — a bigger model does not fix it |
| 5 | Three mechanisms | Cross-references at index time · role tags · a planning step that may decline to search |
| 6 | Four agents, and a loop | The Verifier is adversarial by construction and is the only node that can loop |
| 7 | Knowing when not to answer | Citation resolution is deterministic; eight escalation reasons |
| 8 | Ten services | Each boundary justified; four boundaries rejected and why |
| 9 | Nothing reaches an agent unvalidated | `agent_run_started` is typed `Literal[False]` |
| 10 | How it is evaluated | Escalating is **not** counted as a hallucination |
| 11 | The objective, declared before measuring | Fails at ~640 callers; the 40-thread pool is responsible |
| 12 | Where the tokens go | The context block is the cost; the run cache is −95% on repeats and nothing on unique traffic |
| 13 | **Seven incidents** | Six of them reported success — that is this system's real failure mode |
| 14 | What is not finished | Volunteer the gaps before anyone finds them |
| 15 | Demonstration | `scripts/demo.sh` |

Slides 3 and 13 are the two to spend time on. Slide 3 is the problem the whole
design answers; slide 13 is the engineering-judgement slide and is worth more
than any feature slide.

## Before presenting

```bash
scripts/smoke.sh      # PASS/FAIL for all ten demonstration items
scripts/demo.sh       # the section 4.1 case, end to end
```

Run both before the session so nothing is discovered live.

## Rebuilding it

The deck is generated, not hand-made, so every figure on it traces to one place
in the repository — [evaluation.md](../evaluation.md),
[load-test.md](../load-test.md), [cost.md](../cost.md) and
[INCIDENT_LOG.md](../../INCIDENT_LOG.md).

```bash
cd docs/presentation
npm install pptxgenjs
node build_deck.js              # writes claim-triage-deck.pptx
python qa_deck.py claim-triage-deck.pptx
```

`build_deck.js` expects `apply_theme.js` beside it, or `PPTX_SKILL_DIR` set to
a directory containing `scripts/apply_theme.js`. Without it the deck still
builds but the scheme colours fall back to Office's stock palette.

## A note on QA

Three checks were run and two of them are conclusive:

| check | tool | result |
|---|---|---|
| file validity | `validate.py` | **passed** — schema, relationships, content types, charts |
| content | `slide_text.py` | **passed** — 15 slides, no placeholder text left behind |
| geometry | `qa_deck.py` | **passed** — no overflow, nothing off-slide, no overlapping text boxes |
| **visual** | — | **not done** |

The machine this was built on has neither LibreOffice nor Poppler, so the
slides could not be rendered and looked at. `qa_deck.py` is the substitute: it
reads every shape's real geometry out of the file and estimates whether its
text fits, using a deliberately pessimistic average-glyph-width model so it
warns early rather than missing an overflow.

That catches the defect class a visual pass is mostly looking for, but it
cannot see colour contrast, awkward line breaks, or a chart that simply looks
wrong. **Open the deck once before presenting it.** Seventeen shapes are
flagged "tight" (85–93% of their box) — all expected to be fine, all worth a
glance.
