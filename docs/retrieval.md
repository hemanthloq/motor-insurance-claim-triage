# Chunking, embedding and retrieval (L1)

## Why this corpus is hard

The brief says a policy wording "defines terms in one place, states the cover in
another, and lists restrictions and exclusions in a third", and that an answer
taken from the cover section alone is often wrong. In the ICICI Lombard private
car wording that is concretely true:

| what | where | page |
|---|---|---|
| the own-damage grant | Chapter 1, Section I | 1 |
| the third-party grant | Chapter 1, Section II | 2 |
| the exclusions that apply to *every* section | Chapter 2, General Exceptions | 3 |
| the duties on the insured, precedent to liability | Chapter 3, Conditions | 3–4 |
| the purposes the vehicle may be used for | Chapter 4, Limitations as to Use | 19 |
| who may drive | Chapter 5, Driver's Clause | 19 |
| how a claim is made and assessed | Chapter 6, Claim Procedure | 19 |
| the IMT endorsements — 14 pages of them | Chapter 3 (sic) | 8–22 |

Two properties of that structure drive every decision below. **The provisions
that decide an outcome are a long way from the ones that look relevant**, and
**the document contains far more text that is irrelevant to any given claim than
text that is relevant** — the IMT endorsement chapter alone is a third of the
document and applies only when an endorsement was taken.

## Three passes, not a splitter

### Pass 0: repair the text

This turned out to be the highest-leverage code in the project, and it is worth
being specific about why.

Policy wordings are typeset in narrow columns, so the PDF extractor emits a hard
newline mid-sentence. Left alone, `hire or reward` is split across a line break
and the lexical index stops matching the phrase that the whole section 4.1 case
turns on. So wrapped lines have to be joined.

But join too eagerly and `CHAPTER 4 - Limitations as to Use` is absorbed into the
paragraph above it, the chunker never sees a heading, and **every clause in that
chapter is filed under the wrong section path.** That is exactly what happened on
the first pass, and the cause was mundane: the PDF leaves a *trailing space*
after the preceding sentence, so a "previous character was a full stop" test
misses it.

`loaders._should_join` therefore decides per line pair, on four rules: the
previous line ending a sentence, the current line opening a structural element,
the previous line being a `CHAPTER`/`SECTION` heading (which rarely ends in a
full stop), and a change in capitalisation case between the two.

The other half of pass 0 is `strip_running_lines`. `PRIVATE CAR PACKAGE POLICY
WORDING` sits at the top of all 46 pages. Left in, the chunker reads it as an
all-caps heading once per page and files every clause under it. Any line
appearing on 30% or more of pages is page furniture and is removed. This single
function is worth more to retrieval quality than any amount of tuning
downstream.

Both are tested directly, including the specific regression:
`tests/test_retrieval.py::test_heading_is_not_absorbed_into_the_paragraph_above`.

### Pass 1: segment on the document's own hierarchy

Chapter, section, roman-numbered clause and lettered sub-clause markers are
recognised explicitly, and each leaf gets a `section_path`:

```
Chapter 2 - Exception, Condition & Deductible > 1. GENERAL EXCEPTIONS > III > a
```

All-caps headings are accepted only when the line *starts a block* (the line
above is absent, blank, or ends a sentence), and `CHAPTER`/`SECTION` headings
only when what follows the separator reads as a title rather than as prose —
`_is_title_like` counts the capitalised share of content words. Without that
test the pattern also swallows mid-sentence cross-references like *"Section
II-I(C) but subject otherwise to the terms exceptions"* and the section tree
collapses onto them.

**Why not a fixed-size splitter.** A policy wording is a tree, not prose. A
fixed-size splitter cuts across it and you get a chunk containing the back half
of an exclusion and the front half of a definition. Retrieve it and you cite
text that does not say what you claim it says — which, for a system whose entire
output is "here is the clause", is the worst available failure.

### Pass 2: pack into chunks without crossing a heading

Short sibling leaves are packed together; a leaf longer than the budget is split
on sentence boundaries with a 64-token overlap carried as a tail, so a clause cut
in two keeps its subject. A chunk therefore always belongs to exactly one section
path.

Budget: `CHUNK_TOKENS=320`, `CHUNK_OVERLAP_TOKENS=64`. Tokens are estimated at
four characters each rather than measured with a real tokeniser — the budget is a
soft target, and a real tokeniser would pin the chunker to one model family.

### Pass 3: tag and cross-link

**Role.** Every chunk gets one of `grant`, `restriction`, `exclusion`,
`condition`, `procedure`, `definition`, inferred from its heading path and its
own wording (`ROLE_RULES`). On the real corpus:

| document | chunks | grant | condition | exclusion | restriction | procedure | definition |
|---|---|---|---|---|---|---|---|
| policy wording (46 pp) | 353 | 159 | 89 | 48 | 20 | 19 | 18 |
| product & claims (8 pp) | 28 | 9 | 10 | 5 | — | — | 4 |

That tag is what makes a role-filtered search possible, and a role-filtered
search is the only way to ask *"what, anywhere in this document, could take the
cover away?"* — a question no phrasing of a claim description will answer.

**Cross-references.** Each chunk records the names of other clauses it points at
(`NAMED_CLAUSES`). The general exception that says loss is excluded while the
vehicle is used "otherwise than in accordance with the 'Limitations as to Use'"
therefore carries:

```json
"references": ["Limitations as to Use", "Driver's Clause"]
```

This is the mechanism the section 4.1 case rests on. The agent reads one clause,
sees what it names, and fetches that by name with `find_named_clause` — which is
how a claims handler actually works through a wording, and does not depend on an
embedding of "food delivery accident" landing near a sentence about hire and
reward. See [difficult-case.md](difficult-case.md).

## What gets embedded

Not the raw chunk. This:

```
ICICI Lombard - Private Car Package Policy - Policy Wording > CHAPTER 4 - Limitations as to Use
[role: restriction]
The Policy covers use of the vehicle for any purpose other than: Hire or Reward, ...
```

Policy clause bodies are full of unresolved references — "such person", "the said
occurrences", "this endorsement". Prefixing the document title and the heading
path restores the context the body assumes, and costs about twenty tokens per
chunk. The role tag is included so the embedding space itself carries some
separation between a grant and an exclusion.

## Embedding backend

`sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions, run **locally**.
About 90 MB, downloaded once into the `index-data` volume. No API key, no
per-token cost. The cost section of the brief is right that embedding a 46-page
wording through a paid API is the only operation here with a real bill attached,
so we do not.

A `hash` backend exists as a fallback: a hashed character-trigram projection,
deterministic, dependency-free, dimensionally identical. It keeps the service up
and keeps the lexical half of the retriever working when the model cannot be
downloaded — an air-gapped lab machine, a first-run network failure, CI. It is
**genuinely useless for semantic similarity**, and `/health/ready` reports which
backend is live so it can never be mistaken for working:

```json
{ "corpus_index": { "ok": true, "chunks": 381, "embeddings": "hash" } }
```

Every number in this repository's own testing was taken with the `hash` backend,
which is one of the reasons [evaluation.md](evaluation.md) is explicit that none
of them measure answer quality.

The embedding signature — `backend:model:dim` — is part of the index version, so
switching backends invalidates the index rather than silently mixing two
incompatible spaces.

## Search: hybrid, fused with RRF

Dense cosine over the embedding space **and** BM25 over the raw clause text,
fused with reciprocal rank fusion.

The reason is specific to this corpus rather than general good practice. Policy
wordings turn on exact terms of art — *hire or reward*, *limitations as to use*,
*pace making*. A 384-dimension sentence embedding smears those into a
neighbourhood of generally-insurance-ish text; BM25 matches them precisely.
Conversely a customer's description — *"I was dropping off a food order"* —
shares no tokens at all with the clause that governs it, and only the dense side
can bridge that. **Neither half is sufficient and their failure modes are
complementary**, so we run both.

RRF rather than a weighted score sum because the two scales are not comparable:
cosine is bounded in [−1, 1], BM25 is unbounded and corpus-dependent, and
normalising them would mean choosing a weight we have no principled way to set on
a corpus this size.

Three structural accessors sit on top of search — `get_clause`, `neighbours`,
`find_named_clause` — so the agent can *navigate* rather than guess.

## Re-embedding without rebuilding (L1)

The pipeline is keyed on the SHA-256 of each source file.

* Re-running over an unchanged corpus re-parses nothing and re-embeds nothing.
* A document whose hash has moved is **dropped and re-indexed on its own**:
  `drop_document(document_id)` then `upsert_chunks`.
* Clause ids are `{document_id}::{section_slug}::{ordinal}`, so one document's
  ids never collide with another's and re-indexing one leaves the others' ids
  untouched — which matters because citations are stored by id.

The unit of invalidation is a source document because that is the unit in which
an insurer republishes a wording. Tested by
`test_single_document_can_be_dropped_and_reindexed`.

Both backends support this. Chroma has native `upsert` and `delete(where=...)`;
the local numpy store rewrites its matrix and JSONL sidecar atomically.

## Freshness (D2)

Every ingest run records, to Redis *and* to `index_meta.json` beside the vectors:

```json
{
  "index_version": "e4956b50fed7ea94",
  "built_at": "2026-10-14T09:12:03Z",
  "documents": [ { "document_id": "...", "content_sha256": "...", "pages": 46,
                   "chunks": 353, "roles": {...} } ],
  "chunks": 381,
  "embedding_signature": "sentence_transformers:all-MiniLM-L6-v2:384",
  "build_duration_seconds": 5.1,
  "chunks_per_second": 74.57
}
```

Exposed on `GET /health/corpus`, and as gauges `triage_index_chunks`,
`triage_index_documents`, `triage_index_built_timestamp_seconds`,
`triage_index_age_seconds` and `triage_ingest_chunks_per_second`. The
`CorpusIndexStale` alert fires above seven days, `CorpusIndexEmpty` immediately —
an empty index means every run escalates, which is safe but useless.

`index_version` is written into every result, so an answer can always be traced
back to the exact document versions it was grounded in.

## Caching (D7)

Three layers, all in Redis, all keyed on something that would change the answer:

| layer | key | why |
|---|---|---|
| embedding | `signature + query text` | the agent re-issues near-identical queries on its second pass, and the same questions recur across a claims book |
| retrieval | `query + k + role + doc + signature + index_version` | same, one level up; includes the index version so a re-index invalidates it |
| whole run | `claim body + prompt fingerprint + index_version` | deliberately **not** the claim reference: two references describing the same incident should hit, the same reference after a re-index should not |

Measured effect is in [cost.md](cost.md). The short version: the run cache is
worth 95% of latency and 95% of tokens on repeated traffic and nothing at all on
unique traffic; the embedding and retrieval caches buy about 5% of tokens and no
measurable latency when embeddings are local. That is a less flattering result
than "caching helps" and it is the one we measured.
