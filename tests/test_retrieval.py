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


@pytest.mark.parametrize("pages", [3, 10])
def test_page_numbers_are_stripped_however_short_the_document(pages):
    """'Page 3 of 8' is different on every page, so the repetition test never
    caught it, and it was glued onto the front of a chunk the agent then cited
    as a grant. A short document skipped the stripping altogether."""
    raw = [LoadedPage(number=n, text=f"body of page {n}\nPage {n} of {pages}\n{n}/{pages}")
           for n in range(1, pages + 1)]
    cleaned = strip_running_lines(raw)
    assert [p.text for p in cleaned] == [f"body of page {n}" for n in range(1, pages + 1)]


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


GRANT_WITH_ITEMS = """SECTION I. LOSS OF OR DAMAGE TO THE VEHICLE INSURED
The Company will indemnify the insured against loss or damage to the vehicle insured hereunder and/or its accessories whilst thereon
i. by fire explosion self ignition or lightning;
ii. by burglary housebreaking or theft;
iii. by riot and strike;
iv. by accidental external means, including where (a) the vehicle is in transit by road rail or inland waterway, or (b) it is being driven by an authorised driver whose licence is current at the time;
SECTION II. LIABILITY TO THIRD PARTIES
The Company will indemnify the insured in the event of an accident.
"""


@pytest.fixture(scope="module")
def grant_chunks(tmp_path_factory):
    path = tmp_path_factory.mktemp("grant") / "grant.txt"
    path.write_text(GRANT_WITH_ITEMS, encoding="utf-8")
    return chunk_document(load(path, document_id="g", title="Grant"))


def test_a_grant_and_its_numbered_perils_stay_one_clause(grant_chunks):
    """The own-damage grant used to be shredded: each roman item became its own
    'section', so the lead-in ("will indemnify ... against loss or damage") sat
    in one chunk and the perils it covers in four others, each nested under the
    one before (i > ii > iii). Neither half answers a coverage question."""
    section_i = [c for c in grant_chunks if c.section_path.startswith("SECTION I.")]
    assert len(section_i) == 1, [c.section_path for c in grant_chunks]
    text = section_i[0].text
    assert "will indemnify" in text
    for marker in ("i. by fire", "ii. by burglary", "iii. by riot", "iv. by accidental"):
        assert marker in text
    assert section_i[0].role == "grant"


def test_lettered_markers_stay_inline(grant_chunks):
    assert not any(c.section_path.endswith(("(a)", "(b)")) for c in grant_chunks)
    assert any("(a) the vehicle is in transit" in c.text for c in grant_chunks)


def test_the_next_section_is_a_sibling_not_a_child(grant_chunks):
    section_ii = [c for c in grant_chunks if "LIABILITY TO THIRD PARTIES" in c.section_path]
    assert section_ii and all(c.section_path.startswith("SECTION II.") for c in section_ii)


@pytest.mark.parametrize("text, is_definition", [
    ("Insured Declared Value shall mean the value fixed at the commencement of the policy.", True),
    ("Accessories means any part fitted to the vehicle by the manufacturer.", True),
    ("For the purpose of this policy a private car is a four-wheeled vehicle.", True),
    ("iv. by accidental external means;", False),
    ("The vehicle was moved by means of a tow rope.", False),
])
def test_definition_detection_is_not_triggered_by_any_use_of_means(text, is_definition):
    """'means' alone tagged 'by accidental external means' -- the core of the
    own-damage grant -- as a definition."""
    from claimtriage.retrieval.chunker import ROLE_RULES
    pattern = dict(ROLE_RULES)["definition"]
    assert bool(pattern.search(text)) is is_definition


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
    """Order comes from rank fusion; the score is absolute evidence. The two can
    disagree -- a clause can rank second on fusion while carrying the highest
    single cosine -- so this asserts the bounds, not the sort."""
    hits = built_index.search("loss or damage to the vehicle insured", top_k=4)
    assert hits
    assert all(0.0 <= h.score <= 1.0 for h in hits)


@pytest.mark.parametrize("query", [
    "loss of or damage to the vehicle insured accidental external means",
    "use for hire or reward carriage of goods",
    "notice of claim conditions",
])
def test_fused_order_does_not_depend_on_top_k(built_index, query):
    """Asking for more results must only append to the list, never reorder it.

    Candidate pools used to be `top_k * 3` deep, so a clause just outside the
    dense pool at k=6 lost its dense vote and dropped out, then ranked third at
    k=8. On the real corpus that clause was the own-damage grant, and the
    section 4.1 verdict cited a prospectus line about EMI instalments instead.
    """
    short = [h.clause_id for h in built_index.search(query, top_k=3)]
    long = [h.clause_id for h in built_index.search(query, top_k=8)]
    assert long[: len(short)] == short


def test_fusion_candidate_depth_is_independent_of_top_k(built_index, monkeypatch):
    """The synthetic corpus is too small for the order test above to catch a
    regression -- every chunk fits in any pool -- so pin the mechanism too."""
    depths: list[int] = []
    real_query = built_index.store.query

    def spy(vector, top_k, where=None):
        depths.append(top_k)
        return real_query(vector, top_k, where)

    monkeypatch.setattr(built_index.store, "query", spy)
    built_index.search("candidate depth probe one", top_k=2)
    built_index.search("candidate depth probe two", top_k=8)
    assert len(depths) == 2 and depths[0] == depths[1], depths


def test_scores_are_absolute_not_normalised_to_one(built_index):
    """The weak-retrieval escalation depends on this.

    An earlier version reported `rrf / best_rrf`, which made the top hit 1.0 for
    every query -- a perfect match and pure gibberish alike -- so the check
    `best < ESCALATION_MIN_RETRIEVAL_SCORE` could never fire.
    """
    from claimtriage.config import get_settings

    floor = get_settings().escalation_min_retrieval_score
    good = max(h.score for h in built_index.search("hire or reward carriage of goods", top_k=3))
    junk_hits = built_index.search("zzqx florp quantum blockchain", top_k=3)
    junk = max((h.score for h in junk_hits), default=0.0)
    assert good > floor, f"a strong query scored {good}, at or below the escalation floor {floor}"
    assert junk < floor, f"gibberish scored {junk}; the weak-retrieval escalation can never fire"
    assert good > junk


def test_a_long_lived_index_sees_a_rebuild_done_elsewhere(tmp_path, synthetic_corpus):
    """The documented order is `docker compose up` then `ingest`. The workers
    start with an empty index and must notice when another process fills it --
    before this was fixed, `find_named_clause` stayed empty for their lifetime
    and the section 4.1 mechanism silently stopped working."""
    import os

    from claimtriage.config import reload_settings
    from claimtriage.retrieval.index import ClauseIndex
    from claimtriage.retrieval.ingest import run_ingest
    from claimtriage.store import reset_backend

    old = {k: os.environ.get(k) for k in ("INDEX_DIR", "CORPUS_MANIFEST")}
    os.environ["INDEX_DIR"] = str(tmp_path / "fresh-index")
    os.environ["CORPUS_MANIFEST"] = str(synthetic_corpus / "sources.yaml")
    reload_settings()
    reset_backend()
    try:
        server = ClauseIndex()  # the long-lived worker, started before any ingest
        server.freshness_interval = 0.0
        assert server.size == 0
        assert server.find_named_clause("Limitations as to Use") == []

        run_ingest(force=True, index=ClauseIndex())  # `tools ingest`, a separate instance

        hits = server.find_named_clause("Limitations as to Use")
        assert hits, "the running index never noticed the corpus had been built"
        assert server.size > 0
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()
        reset_backend()


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


def test_a_chapter_title_does_not_decide_the_role_of_everything_under_it():
    from claimtriage.retrieval.chunker import _infer_role

    path = "Chapter 2 - Exception, Condition & Deductible > 3. CONDITIONS"
    text = "This Policy and the Schedule shall be read together."
    assert _infer_role(path, text) == "condition"
    # with nothing nearer to go on, the chapter title still counts
    assert _infer_role("Chapter 2 - Exception, Condition & Deductible > IV", "It is agreed.") == "exclusion"
