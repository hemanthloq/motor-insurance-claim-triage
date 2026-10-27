"""Vector store backends.

Two implementations behind one interface:

``chroma_http``
    A Chroma server running as its own container. This is the deployment
    configuration. It is a separate service because the index has a different
    resource profile and a different release cadence from the agent workers:
    re-indexing the corpus must not require a worker deploy, and three workers
    scaled out under `--scale` must share one index rather than each holding a
    private copy in memory.

``local``
    A numpy matrix plus a JSONL sidecar on disk. Used by `make dev` and by the
    tests, where standing up a second container buys nothing.

Both support per-document upsert and delete, which is the requirement behind
L1's "re-embedding must be possible without rebuilding the whole index": the
unit of invalidation is a source document, because that is the unit in which an
insurer republishes a wording.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger("vectorstore")


class VectorStore:
    """Interface. All ids are clause ids."""

    def upsert(self, ids: Sequence[str], vectors: np.ndarray, metadatas: Sequence[dict], documents: Sequence[str]) -> None:
        raise NotImplementedError

    def delete_document(self, document_id: str) -> int:
        raise NotImplementedError

    def query(self, vector: np.ndarray, top_k: int, where: dict | None = None) -> list[dict]:
        raise NotImplementedError

    def get(self, clause_id: str) -> dict | None:
        raise NotImplementedError

    def all_records(self) -> list[dict]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def healthy(self) -> bool:
        return True


# -----------------------------------------------------------------------------
# Local numpy store
# -----------------------------------------------------------------------------


class LocalVectorStore(VectorStore):
    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.vectors_path = self.dir / "vectors.npy"
        self.records_path = self.dir / "chunks.jsonl"
        self._records: list[dict] = []
        self._matrix: np.ndarray = np.zeros((0, 0), dtype=np.float32)
        self._load()

    def _load(self) -> None:
        if self.records_path.exists():
            with self.records_path.open("r", encoding="utf-8") as handle:
                self._records = [json.loads(line) for line in handle if line.strip()]
        if self.vectors_path.exists():
            self._matrix = np.load(self.vectors_path)
        if len(self._records) != self._matrix.shape[0]:
            log.warning(
                "index_sidecar_mismatch",
                records=len(self._records),
                vectors=int(self._matrix.shape[0]),
                action="treating index as empty; rebuild with `ingest`",
            )
            self._records, self._matrix = [], np.zeros((0, 0), dtype=np.float32)

    def _persist(self) -> None:
        tmp_records = self.records_path.with_suffix(".jsonl.tmp")
        with tmp_records.open("w", encoding="utf-8") as handle:
            for record in self._records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        tmp_records.replace(self.records_path)
        # np.save appends ".npy" unless the handle is opened explicitly, which
        # would defeat the atomic rename.
        tmp_vectors = self.vectors_path.with_name(self.vectors_path.name + ".tmp")
        with tmp_vectors.open("wb") as handle:
            np.save(handle, self._matrix)
        tmp_vectors.replace(self.vectors_path)

    def upsert(self, ids, vectors, metadatas, documents) -> None:
        incoming = {cid: i for i, cid in enumerate(ids)}
        keep_rows, keep_records = [], []
        for row, record in enumerate(self._records):
            if record["clause_id"] not in incoming:
                keep_rows.append(row)
                keep_records.append(record)
        base = self._matrix[keep_rows] if keep_rows and self._matrix.size else np.zeros(
            (0, vectors.shape[1]), dtype=np.float32
        )
        new_records = [
            {**dict(meta), "clause_id": cid, "text": text}
            for cid, meta, text in zip(ids, metadatas, documents)
        ]
        self._records = keep_records + new_records
        self._matrix = np.vstack([base, np.asarray(vectors, dtype=np.float32)]) if len(
            new_records
        ) else base
        self._persist()

    def delete_document(self, document_id: str) -> int:
        keep_rows, keep_records = [], []
        for row, record in enumerate(self._records):
            if record.get("document_id") != document_id:
                keep_rows.append(row)
                keep_records.append(record)
        removed = len(self._records) - len(keep_records)
        if removed:
            self._matrix = (
                self._matrix[keep_rows]
                if keep_rows
                else np.zeros((0, self._matrix.shape[1] if self._matrix.size else 0), dtype=np.float32)
            )
            self._records = keep_records
            self._persist()
        return removed

    def query(self, vector, top_k, where=None):
        if not self._records or self._matrix.size == 0:
            return []
        candidates = range(len(self._records))
        if where:
            candidates = [
                i
                for i in candidates
                if all(_match(self._records[i].get(k), v) for k, v in where.items())
            ]
            if not candidates:
                return []
        rows = np.asarray(list(candidates), dtype=int)
        sub = self._matrix[rows]
        query_vector = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query_vector))
        if norm:
            query_vector = query_vector / norm
        scores = sub @ query_vector
        order = np.argsort(-scores)[:top_k]
        return [
            {**self._records[int(rows[i])], "score": float(scores[int(i)])}
            for i in order
        ]

    def get(self, clause_id):
        for record in self._records:
            if record["clause_id"] == clause_id:
                return dict(record)
        return None

    def all_records(self):
        return [dict(record) for record in self._records]

    def count(self):
        return len(self._records)


def _match(value: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if "$in" in expected:
            return value in expected["$in"]
        if "$ne" in expected:
            return value != expected["$ne"]
    return value == expected


# -----------------------------------------------------------------------------
# Chroma HTTP store
# -----------------------------------------------------------------------------


class ChromaVectorStore(VectorStore):
    def __init__(self, host: str, port: int, collection: str) -> None:
        import chromadb

        self._client = chromadb.HttpClient(host=host, port=port)
        self._collection = self._client.get_or_create_collection(
            name=collection, metadata={"hnsw:space": "cosine"}
        )
        self._cache: list[dict] | None = None

    def upsert(self, ids, vectors, metadatas, documents) -> None:
        batch = 256
        ids = list(ids)
        metadatas = [_flatten(m) for m in metadatas]
        documents = list(documents)
        matrix = np.asarray(vectors, dtype=np.float32)
        for start in range(0, len(ids), batch):
            end = start + batch
            self._collection.upsert(
                ids=ids[start:end],
                embeddings=matrix[start:end].tolist(),
                metadatas=metadatas[start:end],
                documents=documents[start:end],
            )
        self._cache = None

    def delete_document(self, document_id: str) -> int:
        before = self.count()
        self._collection.delete(where={"document_id": document_id})
        self._cache = None
        return max(0, before - self.count())

    def query(self, vector, top_k, where=None):
        result = self._collection.query(
            query_embeddings=[np.asarray(vector, dtype=np.float32).tolist()],
            n_results=top_k,
            where=where or None,
            include=["metadatas", "documents", "distances"],
        )
        out = []
        ids = (result.get("ids") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        for cid, meta, text, dist in zip(ids, metas, docs, dists):
            out.append({**(meta or {}), "clause_id": cid, "text": text, "score": 1.0 - float(dist)})
        return out

    def get(self, clause_id):
        result = self._collection.get(ids=[clause_id], include=["metadatas", "documents"])
        ids = result.get("ids") or []
        if not ids:
            return None
        meta = (result.get("metadatas") or [{}])[0] or {}
        text = (result.get("documents") or [""])[0]
        return {**meta, "clause_id": ids[0], "text": text}

    def all_records(self):
        if self._cache is not None:
            return [dict(r) for r in self._cache]
        result = self._collection.get(include=["metadatas", "documents"])
        records = []
        for cid, meta, text in zip(
            result.get("ids") or [],
            result.get("metadatas") or [],
            result.get("documents") or [],
        ):
            records.append({**(meta or {}), "clause_id": cid, "text": text})
        self._cache = records
        return [dict(r) for r in records]

    def count(self):
        try:
            return int(self._collection.count())
        except Exception:
            return 0

    def healthy(self):
        try:
            self._client.heartbeat()
            return True
        except Exception:
            return False


def _flatten(meta: dict) -> dict:
    """Chroma metadata values must be scalars."""
    out = {}
    for key, value in meta.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[key] = "|".join(str(v) for v in value)
        else:
            out[key] = str(value)
    return out


# -----------------------------------------------------------------------------
# Factory
# -----------------------------------------------------------------------------


def build_store() -> VectorStore:
    settings = get_settings()
    if settings.vector_backend == "chroma_http":
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                store = ChromaVectorStore(
                    settings.chroma_host, settings.chroma_port, settings.chroma_collection
                )
                log.info("vector_backend_ready", backend="chroma_http", count=store.count())
                return store
            except Exception as exc:  # the container may still be starting
                last_error = exc
                time.sleep(min(2 ** attempt * 0.5, 5))
        log.warning(
            "chroma_unavailable_falling_back_to_local",
            error=str(last_error),
            index_dir=settings.index_dir,
        )
    store = LocalVectorStore(settings.index_dir)
    log.info("vector_backend_ready", backend="local", count=store.count())
    return store
