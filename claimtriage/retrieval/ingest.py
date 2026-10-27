"""The corpus pipeline: fetch -> parse -> chunk -> embed -> index (D2).

Idempotency
-----------
The pipeline is keyed on the SHA-256 of each source file. Re-running it over an
unchanged corpus re-parses nothing, re-embeds nothing and leaves the index
byte-identical; the only thing that changes is the "last checked" timestamp.
A document whose hash has moved is dropped from the index and re-indexed on its
own, which is the L1 requirement that re-embedding must not mean rebuilding
everything.

Freshness
---------
Every run records when the index was built, from which document versions, how
many chunks it produced and at what rate. That record is written to Redis, to
`index_meta.json` beside the vectors, and exposed both on `/health/corpus` and
as Prometheus gauges, so "is the index stale?" is answerable from the dashboard
rather than by reading logs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import get_settings
from ..logging_setup import get_logger
from ..telemetry import ingest_throughput
from .chunker import chunk_document
from .embeddings import get_embedder
from .index import ClauseIndex, write_index_metadata
from .loaders import NoTextLayer, load, sha256_file

log = get_logger("ingest")


@dataclass
class SourceSpec:
    document_id: str
    title: str
    filename: str
    url: str
    kind: str = "pdf"
    role_hint: str = "policy_wording"
    enabled: bool = True

    @property
    def path(self) -> Path:
        return Path(get_settings().corpus_manifest).parent / "raw" / self.filename


def load_manifest(path: str | None = None) -> list[SourceSpec]:
    manifest_path = Path(path or get_settings().corpus_manifest)
    if not manifest_path.exists():
        raise FileNotFoundError(f"corpus manifest not found: {manifest_path}")
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    specs = []
    for entry in raw.get("documents", []):
        specs.append(SourceSpec(**entry))
    return [s for s in specs if s.enabled]


def fetch_missing(specs: list[SourceSpec], *, force: bool = False) -> list[str]:
    """Download anything not already on disk. Separated from indexing so the
    index can be rebuilt on a machine with no network access."""
    import httpx

    fetched = []
    headers = {"User-Agent": "claim-triage-corpus-fetcher/1.0"}
    for spec in specs:
        target = spec.path
        if target.exists() and not force:
            continue
        if not spec.url:
            log.warning("source_missing_and_no_url", document_id=spec.document_id, path=str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        log.info("fetching_source", document_id=spec.document_id, url=spec.url)
        with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as client:
            response = client.get(spec.url)
            response.raise_for_status()
            target.write_bytes(response.content)
        fetched.append(spec.document_id)
    return fetched


def run_ingest(
    *,
    manifest: str | None = None,
    force: bool = False,
    index: ClauseIndex | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    specs = load_manifest(manifest)
    index = index or ClauseIndex()
    embedder = get_embedder()

    previous = index.metadata()
    previous_hashes = {
        doc["document_id"]: doc["content_sha256"]
        for doc in (previous.get("documents") or [])
    }

    started = time.perf_counter()
    documents_meta: list[dict[str, Any]] = []
    total_chunks = 0
    reindexed: list[str] = []
    skipped: list[str] = []
    failures: list[dict[str, str]] = []

    for spec in specs:
        if not spec.path.exists():
            failures.append(
                {
                    "document_id": spec.document_id,
                    "error": f"file not present: {spec.path}. Run `make corpus` first.",
                }
            )
            continue

        digest = sha256_file(spec.path)
        unchanged = previous_hashes.get(spec.document_id) == digest
        if unchanged and not force:
            existing = next(
                (d for d in previous.get("documents", []) if d["document_id"] == spec.document_id),
                None,
            )
            if existing:
                documents_meta.append(existing)
                total_chunks += int(existing.get("chunks", 0))
                skipped.append(spec.document_id)
                log.info("source_unchanged_skipped", document_id=spec.document_id)
                continue

        try:
            document = load(
                spec.path,
                document_id=spec.document_id,
                title=spec.title,
                source_url=spec.url,
            )
        except NoTextLayer as exc:
            failures.append({"document_id": spec.document_id, "error": str(exc)})
            log.error("source_needs_ocr", document_id=spec.document_id, error=str(exc))
            continue

        chunks = chunk_document(
            document,
            chunk_tokens=settings.chunk_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
        removed = index.drop_document(spec.document_id)
        index.upsert_chunks(chunks)
        reindexed.append(spec.document_id)
        total_chunks += len(chunks)

        roles: dict[str, int] = {}
        for chunk in chunks:
            roles[chunk.role] = roles.get(chunk.role, 0) + 1

        documents_meta.append(
            {
                "document_id": spec.document_id,
                "title": spec.title,
                "source_url": spec.url,
                "filename": spec.filename,
                "role_hint": spec.role_hint,
                "content_sha256": digest,
                "pages": document.page_count,
                "chunks": len(chunks),
                "chunks_replaced": removed,
                "roles": roles,
            }
        )
        log.info(
            "document_indexed",
            document_id=spec.document_id,
            pages=document.page_count,
            chunks=len(chunks),
            replaced=removed,
            roles=roles,
        )

    duration = time.perf_counter() - started
    index.refresh()
    meta = write_index_metadata(
        documents=documents_meta,
        chunks=index.size,
        duration_seconds=duration,
        embedding_signature=embedder.signature,
    )
    meta.update(
        {
            "reindexed": reindexed,
            "skipped_unchanged": skipped,
            "failures": failures,
        }
    )
    ingest_throughput.set(meta.get("chunks_per_second", 0.0))
    log.info(
        "ingest_complete",
        chunks=index.size,
        documents=len(documents_meta),
        reindexed=len(reindexed),
        skipped=len(skipped),
        failures=len(failures),
        seconds=round(duration, 2),
        chunks_per_second=meta.get("chunks_per_second"),
    )
    return meta
