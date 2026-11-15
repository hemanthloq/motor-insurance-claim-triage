"""The retrieval facade the agent's tools sit on.

Search is hybrid: dense cosine over the embedding space, BM25 over the raw
clause text, fused with reciprocal rank fusion. The reason is specific to this
corpus rather than general good practice. Policy wordings turn on exact terms of
art -- "hire or reward", "limitations as to use", "pace making". A 384-dimension
sentence embedding smears those into a neighbourhood of generally-insurance-ish
text, while BM25 matches them precisely. Conversely a customer's description
("I was dropping off a food order") shares no tokens at all with the clause that
governs it, and only the dense side can bridge that. Neither half is sufficient;
the failure modes are complementary, so we run both.

On top of search sit three structural accessors the agent uses to *navigate*
rather than guess: lookup by clause id, lookup of a named clause, and the
neighbours of a clause in document order. These are what turn a cross-reference
inside the text ("otherwise than in accordance with the 'Limitations as to
Use'") into a retrieval the agent can actually perform.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..config import get_settings
from ..logging_setup import get_logger
from ..store import cache_get, cache_put, load_index_meta, save_index_meta
from ..telemetry import (
    index_age,
    index_built_at,
    index_chunks,
    index_documents,
    retrieval_latency,
)
from .chunker import Chunk
from .embeddings import get_embedder
from .vectorstore import VectorStore, build_store

log = get_logger("index")

TOKEN_RE = re.compile(r"[a-z0-9']+")
RRF_K = 60.0


def tokenise(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


@dataclass
class SearchHit:
    clause_id: str
    document_id: str
    document_title: str
    section_path: str
    page: int
    role: str
    text: str
    score: float
    dense_rank: int | None = None
    lexical_rank: int | None = None
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "document_id": self.document_id,
            "document_title": self.document_title,
            "section_path": self.section_path,
            "page": self.page,
            "role": self.role,
            "text": self.text,
            "score": round(self.score, 4),
            "references": self.references,
        }


def _as_hit(record: dict[str, Any], score: float) -> SearchHit:
    refs = record.get("references") or ""
    if isinstance(refs, str):
        refs = [r for r in refs.split("|") if r]
    return SearchHit(
        clause_id=record.get("clause_id", ""),
        document_id=record.get("document_id", ""),
        document_title=record.get("document_title", ""),
        section_path=record.get("section_path", ""),
        page=int(record.get("page") or 0),
        role=record.get("role", "grant"),
        text=record.get("text", ""),
        score=score,
        references=list(refs),
    )


#: BM25 is unbounded, so it is mapped onto [0, 1) by saturation:
#: score / (score + LEXICAL_HALF). A raw score of LEXICAL_HALF maps to 0.5. Six
#: is roughly what a query sharing two distinctive terms with a clause scores on
#: this corpus; a query sharing none scores zero.
LEXICAL_HALF = 6.0

#: How often a long-lived index checks whether the corpus was rebuilt under it.
FRESHNESS_INTERVAL_SECONDS = 5.0

#: Weight of the dense ranking in fusion when the embedder is the hash
#: fallback, which has no semantic signal. See `ClauseIndex._fuse`.
HASH_DENSE_WEIGHT = 0.25

#: Candidates each ranker contributes to fusion, independent of top_k.
FUSION_POOL = 50


def lexical_evidence(bm25_score: float) -> float:
    return max(0.0, bm25_score) / (max(0.0, bm25_score) + LEXICAL_HALF)


class ClauseIndex:
    """Thread-safe read path; writes happen only through the ingest CLI.

    Long-lived: the API and every worker hold one for their whole lifetime,
    while the corpus is rebuilt by a *different* process (`tools ingest`). So
    every read first asks, at most every few seconds, whether the index version
    has moved, and reloads if it has. Without that check the documented startup
    order -- `docker compose up`, then `ingest` -- leaves every worker holding
    the empty index it started with: dense search still works through Chroma,
    so nothing obviously fails, but BM25 and `find_named_clause` silently return
    nothing, and the section 4.1 mechanism quietly stops working.
    """

    def __init__(self, store: VectorStore | None = None) -> None:
        self.settings = get_settings()
        self.store = store or build_store()
        self._lock = threading.RLock()
        self._bm25 = None
        self._bm25_ids: list[str] = []
        self._records_by_id: dict[str, dict] = {}
        self._ordered_ids: list[str] = []
        self._version: str | None = None
        self._last_fresh_check = 0.0
        self.freshness_interval = FRESHNESS_INTERVAL_SECONDS
        self.refresh()

    # -- lifecycle ------------------------------------------------------------

    def _ensure_fresh(self) -> None:
        """Reload if the corpus was rebuilt by another process since we looked."""
        now = time.monotonic()
        if now - self._last_fresh_check < self.freshness_interval:
            return
        self._last_fresh_check = now
        try:
            version = read_index_meta().get("index_version")
        except Exception:
            return
        if version == self._version:
            return
        with self._lock:
            if version == self._version:
                return
            log.info(
                "index_rebuilt_elsewhere_reloading",
                previous_version=self._version,
                new_version=version,
            )
            self.store.reload()
            self.refresh()

    def refresh(self) -> None:
        """Rebuild the in-process lexical index from the vector store."""
        with self._lock:
            self._version = read_index_meta().get("index_version")
            records = self.store.all_records()
            records.sort(key=lambda r: (r.get("document_id", ""), int(r.get("ordinal") or 0)))
            self._records_by_id = {r["clause_id"]: r for r in records}
            self._ordered_ids = [r["clause_id"] for r in records]
            if records:
                try:
                    from rank_bm25 import BM25Okapi

                    # The heading is indexed with the body. It is the most
                    # informative string a clause has -- "SECTION I. LOSS OF OR
                    # DAMAGE TO THE VEHICLE INSURED" -- and an earlier version
                    # indexed body text only, so a search for "loss or damage
                    # to the vehicle insured" ranked prospectus pages and add-ons
                    # above the own-damage grant itself. Dense retrieval already
                    # saw the heading (it is in `embed_text`); BM25 did not.
                    self._bm25 = BM25Okapi([
                        tokenise(f"{r.get('section_path', '')} {r.get('text', '')}") for r in records
                    ])
                    self._bm25_ids = list(self._ordered_ids)
                except Exception as exc:
                    log.warning("bm25_unavailable", error=str(exc))
                    self._bm25 = None
            else:
                self._bm25 = None
                self._bm25_ids = []
            self._publish_freshness()

    def _publish_freshness(self) -> None:
        meta = read_index_meta()
        chunks = self.store.count()
        index_chunks.set(chunks)
        index_documents.set(len(meta.get("documents", [])) or self._distinct_documents())
        built = meta.get("built_at_epoch")
        if built:
            index_built_at.set(float(built))
            index_age.set(max(0.0, time.time() - float(built)))

    def _distinct_documents(self) -> int:
        return len({r.get("document_id") for r in self._records_by_id.values()})

    @property
    def size(self) -> int:
        return self.store.count()

    def healthy(self) -> bool:
        return self.store.healthy() and self.size > 0

    def metadata(self) -> dict[str, Any]:
        meta = dict(read_index_meta())
        meta.setdefault("chunks", self.size)
        built = meta.get("built_at_epoch")
        meta["age_seconds"] = round(time.time() - float(built), 1) if built else None
        meta["embedding_signature"] = get_embedder().signature
        meta["vector_backend"] = type(self.store).__name__
        return meta

    # -- write path -----------------------------------------------------------

    def upsert_chunks(self, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            return
        embedder = get_embedder()
        vectors = embedder.embed_documents([c.embed_text for c in chunks])
        self.store.upsert(
            ids=[c.clause_id for c in chunks],
            vectors=vectors,
            metadatas=[c.to_metadata() for c in chunks],
            documents=[c.text for c in chunks],
        )

    def drop_document(self, document_id: str) -> int:
        return self.store.delete_document(document_id)

    # -- read path ------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        role: str | Sequence[str] | None = None,
        document_id: str | None = None,
        strategy: str = "hybrid",
    ) -> list[SearchHit]:
        top_k = top_k or self.settings.retrieval_top_k
        started = time.perf_counter()
        self._ensure_fresh()

        cache_digest = None
        if self.settings.cache_retrievals:
            payload = json.dumps(
                {
                    "q": query,
                    "k": top_k,
                    "role": role,
                    "doc": document_id,
                    "s": strategy,
                    "sig": get_embedder().signature,
                    "v": read_index_meta().get("index_version", "0"),
                },
                sort_keys=True,
                default=str,
            )
            cache_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]
            cached = cache_get("retrieval", cache_digest)
            if cached is not None:
                retrieval_latency.labels(strategy=f"{strategy}_cached").observe(
                    time.perf_counter() - started
                )
                return [_as_hit(r, r.get("score", 0.0)) for r in cached]

        where: dict[str, Any] = {}
        if role:
            where["role"] = {"$in": list(role)} if not isinstance(role, str) else role
        if document_id:
            where["document_id"] = document_id

        # Each ranker contributes a fixed-depth candidate list, not one sized by
        # top_k. With `top_k * 3` the fused order depended on how many results
        # were asked for: at k=6 the own-damage grant fell outside the dense
        # pool, lost its dense vote, and dropped out; at k=8 it ranked third.
        pool = max(FUSION_POOL, top_k)
        dense: list[dict] = []
        if strategy in {"hybrid", "dense"}:
            vector = get_embedder().embed_query(query)
            dense = self.store.query(vector, pool, where or None)

        lexical: list[dict] = []
        if strategy in {"hybrid", "lexical"} and self._bm25 is not None:
            scores = self._bm25.get_scores(tokenise(query))
            ranked = sorted(range(len(scores)), key=lambda i: -scores[i])
            for position in ranked:
                if scores[position] <= 0:
                    break
                record = self._records_by_id.get(self._bm25_ids[position])
                if record is None:
                    continue
                if where and not self._matches(record, where):
                    continue
                lexical.append({**record, "score": float(scores[position])})
                if len(lexical) >= pool:
                    break

        if strategy == "hybrid":
            hits = self._fuse(dense, lexical, top_k)
        elif strategy == "dense":
            hits = [_as_hit(r, max(0.0, min(1.0, r.get("score", 0.0)))) for r in dense[:top_k]]
        else:
            hits = [_as_hit(r, lexical_evidence(r.get("score", 0.0))) for r in lexical[:top_k]]

        retrieval_latency.labels(strategy=strategy).observe(time.perf_counter() - started)
        if cache_digest:
            cache_put("retrieval", cache_digest, [h.to_dict() for h in hits])
        return hits

    @staticmethod
    def _matches(record: dict, where: dict) -> bool:
        for key, expected in where.items():
            value = record.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if value not in expected["$in"]:
                    return False
            elif value != expected:
                return False
        return True

    def _fuse(self, dense: list[dict], lexical: list[dict], top_k: int) -> list[SearchHit]:
        """Reciprocal rank fusion for the *order*; absolute evidence for the *score*.

        RRF rather than a weighted score sum because the two scales are not
        comparable -- cosine is bounded in [-1, 1], BM25 is unbounded and
        corpus-dependent -- and normalising them would mean choosing a weight we
        have no principled way to set on a corpus of this size.

        But RRF only ranks. An earlier version reported `rrf / best_rrf` as the
        hit's score, which makes the top hit 1.0 *by construction* -- for a
        perfect match and for a query of pure gibberish alike. Every consumer of
        that number was then reading a rank, not a relevance: the
        weak-retrieval escalation (`best < ESCALATION_MIN_RETRIEVAL_SCORE`)
        could never fire, and every citation displayed "relevance 1.00".

        So the score each hit carries is now its absolute evidence: the better
        of its dense cosine and its saturated BM25 score. Ordering is unchanged.
        """
        fused: dict[str, float] = {}
        ranks: dict[str, dict[str, int]] = {}
        evidence: dict[str, float] = {}
        # The hash fallback embedder carries no semantic signal -- its docstring
        # says so -- and fusing it at full weight actively degrades a good
        # lexical ranking: the own-damage grant was BM25 rank 4 for "loss or
        # damage to the vehicle insured" and fell out of the hybrid top six,
        # displaced by trigram noise. With a real embedder the weight is 1.0
        # and nothing changes.
        dense_weight = 1.0 if get_embedder().backend != "hash" else HASH_DENSE_WEIGHT
        for rank, record in enumerate(dense, start=1):
            cid = record["clause_id"]
            fused[cid] = fused.get(cid, 0.0) + dense_weight / (RRF_K + rank)
            ranks.setdefault(cid, {})["dense"] = rank
            evidence[cid] = max(evidence.get(cid, 0.0), max(0.0, min(1.0, record.get("score", 0.0))))
        for rank, record in enumerate(lexical, start=1):
            cid = record["clause_id"]
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
            ranks.setdefault(cid, {})["lexical"] = rank
            evidence[cid] = max(evidence.get(cid, 0.0), lexical_evidence(record.get("score", 0.0)))

        if not fused:
            return []
        ordered = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]

        lookup = {r["clause_id"]: r for r in dense}
        lookup.update({r["clause_id"]: r for r in lexical})

        hits: list[SearchHit] = []
        for cid, _rrf in ordered:
            record = lookup.get(cid) or self._records_by_id.get(cid)
            if record is None:
                continue
            hit = _as_hit(record, round(evidence.get(cid, 0.0), 4))
            hit.dense_rank = ranks.get(cid, {}).get("dense")
            hit.lexical_rank = ranks.get(cid, {}).get("lexical")
            hits.append(hit)
        return hits

    # -- structural accessors -------------------------------------------------

    def get_clause(self, clause_id: str) -> SearchHit | None:
        self._ensure_fresh()
        record = self._records_by_id.get(clause_id) or self.store.get(clause_id)
        return _as_hit(record, 1.0) if record else None

    def neighbours(self, clause_id: str, window: int = 1) -> list[SearchHit]:
        self._ensure_fresh()
        try:
            position = self._ordered_ids.index(clause_id)
        except ValueError:
            return []
        lo = max(0, position - window)
        hi = min(len(self._ordered_ids), position + window + 1)
        return [
            _as_hit(self._records_by_id[cid], 1.0)
            for cid in self._ordered_ids[lo:hi]
            if cid != clause_id
        ]

    def find_named_clause(self, name: str) -> list[SearchHit]:
        """Resolve a clause referred to by name in the text of another clause.

        Matched against the heading path first (a clause *is* the named clause),
        then against the body (a clause *mentions* it). Heading matches sort
        first, which is what the caller almost always wants.
        """
        self._ensure_fresh()
        needle = name.strip().lower().rstrip(".")
        if not needle:
            return []
        heading_hits, body_hits = [], []
        for record in self._records_by_id.values():
            section = (record.get("section_path") or "").lower()
            if needle in section:
                heading_hits.append(_as_hit(record, 1.0))
            elif needle in (record.get("text") or "").lower():
                body_hits.append(_as_hit(record, 0.7))
        heading_hits.sort(key=lambda h: int(h.page or 0))
        body_hits.sort(key=lambda h: int(h.page or 0))
        return heading_hits + body_hits

    def quote_resolves(self, clause_id: str, quote: str) -> bool:
        """Does this quote actually appear in this clause? (A3)"""
        self._ensure_fresh()
        record = self._records_by_id.get(clause_id) or self.store.get(clause_id)
        if not record:
            return False
        return _normalise_for_match(quote) in _normalise_for_match(record.get("text", ""))


def _normalise_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def read_index_meta() -> dict[str, Any]:
    """Index metadata, from Redis if it is there and from disk if it is not.

    The metadata is written to both at ingest time. Redis is the fast path that
    every container shares; the JSON file beside the vectors is what survives a
    Redis flush, and is what makes `index-info` work from a CLI container that
    has no Redis at all.
    """
    meta = load_index_meta()
    if meta:
        return meta
    sidecar = Path(get_settings().index_dir) / "index_meta.json"
    if sidecar.exists():
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


# -----------------------------------------------------------------------------
# Index metadata (D2)
# -----------------------------------------------------------------------------


def write_index_metadata(
    *,
    documents: list[dict[str, Any]],
    chunks: int,
    duration_seconds: float,
    embedding_signature: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    version = hashlib.sha256(
        "|".join(sorted(f"{d['document_id']}:{d['content_sha256']}" for d in documents)).encode()
        + embedding_signature.encode()
    ).hexdigest()[:16]
    meta = {
        "index_version": version,
        "built_at": now.isoformat(),
        "built_at_epoch": now.timestamp(),
        "documents": documents,
        "document_count": len(documents),
        "chunks": chunks,
        "embedding_signature": embedding_signature,
        "build_duration_seconds": round(duration_seconds, 3),
        "chunks_per_second": round(chunks / duration_seconds, 2) if duration_seconds else 0.0,
    }
    save_index_meta(meta)
    settings = get_settings()
    Path(settings.index_dir).mkdir(parents=True, exist_ok=True)
    (Path(settings.index_dir) / "index_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return meta


_index: ClauseIndex | None = None
_index_lock = threading.Lock()


def get_index() -> ClauseIndex:
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = ClauseIndex()
    return _index


def reset_index() -> None:
    global _index
    _index = None
