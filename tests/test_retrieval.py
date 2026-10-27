"""Chunking and retrieval (L1).

These tests are about the structural properties the agent depends on, not about
ranking quality. Ranking is measured by the evaluation set against a real model
and a real corpus; what is asserted here is that the index *can* answer the
questions the agent asks of it:

* a chunk belongs to exactly one section of the document's own hierarchy
* a clause that names another clause records that cross-reference
* a named clause can be resolved by name
* a quote can be checked against the clause it is attributed to
* a document can be re-indexed on its own
"""

from __future__ import annotations

import pytest

from claimtriage.retrieval.chunker import chunk_document, estimate_tokens
from claimtriage.retrieval.loaders import load, normalise, strip_running_lines, LoadedPage


# -----------------------------------------------------------------------------
# Text normalisation -- the fix that made everything downstream work
# -----------------------------------------------------------------------------


def test_wrapped_lines_are_joined():
    raw = "The Company will indemnify the insured against\nloss or damage to the vehicle."
    assert normalise(raw) == (
        "The Company will indemnify the insured against loss or damage to the vehicle."
    )


def test_heading_is_not_absorbed_into_the_paragraph_above():
    """The specific bug. The PDF leaves a trailing space after the sentence, so
    a naive 'previous character was a full stop' test misses it and the chapter
    heading is swallowed -- which files every clause in that chapter under the
    wrong section path."""
    raw = (
        "Subject otherwise to the terms conditions limitations and exceptions of this Policy. \n"
        "CHAPTER 4 - Limitations as to Use\n"
        "The Policy covers use of the vehicle for any purpose other than:"
    )
    lines = normalise(raw).split("\n")
    assert "CHAPTER 4 - Limitations as to Use" in lines


def test_chapter_heading_keeps_its_own_line():
    raw = "CHAPTER 4 - Limitations as to Use\nThe Policy covers use of the vehicle"
    assert normalise(raw).split("\n")[0] == "CHAPTER 4 - Limitations as to Use"


def test_hyphenation_across_a_line_break_is_repaired():
    assert "exclusion" in normalise("this exclu-\nsion applies")


def test_running_headers_are_stripped():
    pages = [
        LoadedPage(number=n, text=f"PRIVATE CAR PACKAGE POLICY WORDING\nbody of page {n}\n{n}")
        for n in range(1, 11)
    ]
    cleaned = strip_running_lines(pages)
    assert all("PRIVATE CAR PACKAGE POLICY WORDING" not in p.text for p in cleaned)
    assert all(f"body of page {p.number}" in p.text for p in cleaned)


def test_token_estimate_is_monotonic():
    assert estimate_tokens("a") < estimate_tokens("a" * 100) < estimate_tokens("a" * 1000)


# -----------------------------------------------------------------------------
# Chunking
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chunks(synthetic_corpus):
    document = load(
        synthetic_corpus / "raw" / "test_wording.txt",
        document_id="test_wording",
        title="Test Insurer - Private Car Package Policy - Policy Wording",
    )
    return chunk_document(document, chunk_tokens=320, overlap_tokens=64)


def test_chunks_are_produced_with_stable_ids(chunks):
    assert chunks
    ids = [c.clause_id for c in chunks]
    assert len(ids) == len(set(ids)), "clause ids must be unique"
    assert all(c.clause_id.startswith("test_wording::") for c in chunks)


def test_every_chunk_has_a_section_path_and_a_role(chunks):
    roles = {"grant", "restriction", "exclusion", "condition", "procedure", "definition"}
    for chunk in chunks:
        assert chunk.section_path
        assert chunk.role in roles
        assert chunk.text.strip()


def test_limitations_chapter_is_its_own_section(chunks):
    matching = [c for c in chunks if "limitations as to use" in c.section_path.lower()]
    assert matching, [c.section_path for c in chunks]
    body = " ".join(c.text for c in matching).lower()
    assert "hire or reward" in body
    assert "carriage of goods" in body


def test_general_exception_records_its_cross_reference(chunks):
    """The mechanism the section 4.1 case turns on.

    The exception names the restriction. Recording that at index time is what
    lets the agent navigate to it rather than hope an embedding finds it.
    """
    referring = [c for c in chunks if "Limitations as to Use" in c.references]
    assert referring, "no chunk recorded a reference to the Limitations as to Use"
    assert any("otherwise than in accordance" in c.text.lower() for c in referring)


def test_embed_text_carries_the_section_path(chunks):
    chunk = chunks[0]
    assert chunk.section_path in chunk.embed_text
    assert f"[role: {chunk.role}]" in chunk.embed_text
    assert chunk.text in chunk.embed_text


def test_chunks_respect_the_token_budget(chunks):
    # Allow headroom: a single indivisible sentence may exceed the target.
    assert all(c.token_estimate <= 320 * 1.6 for c in chunks), [
        (c.clause_id, c.token_estimate) for c in chunks if c.token_estimate > 512
    ]


# -----------------------------------------------------------------------------
# Index
# -----------------------------------------------------------------------------


def test_index_is_populated(built_index):
    assert built_index.size > 0
    assert built_index.healthy()


def test_named_clause_resolution(built_index):
    hits = built_index.find_named_clause("Limitations as to Use")
    assert hits
    # A clause that *is* the named clause sorts ahead of one that mentions it.
    assert "limitations as to use" in hits[0].section_path.lower()


def test_lexical_search_finds_terms_of_art(built_index):
    hits = built_index.search("hire or reward carriage of goods", strategy="lexical", top_k=3)
    assert hits
    assert any("hire or reward" in h.text.lower() for h in hits)


def test_role_filter_narrows_to_restrictions(built_index):
    hits = built_index.search(
        "permitted purposes for which the vehicle may be used",
        role=["restriction", "exclusion"],
        top_k=5,
    )
    assert hits
    assert all(h.role in {"restriction", "exclusion"} for h in hits)


def test_hybrid_search_returns_scored_hits(built_index):
    hits = built_index.search("loss or damage to the vehicle insured", top_k=4)
    assert hits
    assert all(0.0 <= h.score <= 1.0 for h in hits)
    assert hits == sorted(hits, key=lambda h: -h.score)


def test_get_clause_and_neighbours(built_index):
    first = built_index.search("accidental external means", top_k=1)[0]
    fetched = built_index.get_clause(first.clause_id)
    assert fetched is not None
    assert fetched.clause_id == first.clause_id
    neighbours = built_index.neighbours(first.clause_id, window=1)
    assert all(n.clause_id != first.clause_id for n in neighbours)


def test_quote_resolution_is_exact_but_whitespace_tolerant(built_index):
    hit = built_index.find_named_clause("Limitations as to Use")[0]
    words = hit.text.split()
    verbatim = " ".join(words[:8])
    assert built_index.quote_resolves(hit.clause_id, verbatim)
    # whitespace differences are tolerated; different words are not
    assert built_index.quote_resolves(hit.clause_id, verbatim.replace(" ", "   "))
    assert not built_index.quote_resolves(hit.clause_id, "the Company hereby approves this claim")


def test_quote_resolution_rejects_an_unknown_clause(built_index):
    assert not built_index.quote_resolves("no_such_doc::nowhere::0001", "anything at all")


def test_single_document_can_be_dropped_and_reindexed(built_index, synthetic_corpus):
    """L1: re-embedding must be possible without rebuilding the whole index."""
    from claimtriage.retrieval.chunker import chunk_document
    from claimtriage.retrieval.loaders import load

    before = built_index.size
    removed = built_index.drop_document("test_wording")
    assert removed == before
    assert built_index.store.count() == 0

    document = load(
        synthetic_corpus / "raw" / "test_wording.txt",
        document_id="test_wording",
        title="Test Insurer - Private Car Package Policy - Policy Wording",
    )
    built_index.upsert_chunks(chunk_document(document))
    built_index.refresh()
    assert built_index.size == before


def test_index_metadata_records_freshness(built_index):
    meta = built_index.metadata()
    assert meta.get("index_version")
    assert meta.get("chunks")
    assert meta.get("embedding_signature")
    assert "hash" in meta["embedding_signature"]
