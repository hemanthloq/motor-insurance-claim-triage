"""Embeddings (L1, cost section of the brief).

Embedding happens locally with sentence-transformers all-MiniLM-L6-v2: 384
dimensions, ~90 MB, no API key, no per-token cost. Embedding a 46-page wording
through a paid API would be the only operation in this project with a real
bill attached, so we do not.

A deterministic `hash` backend is available as a fallback. It is a hashed
bag-of-character-ngrams projection -- genuinely useless for semantic similarity,
but it is stable, dependency-free and dimensionally identical, so the service
still starts and the lexical half of the hybrid retriever still works when the
model cannot be downloaded (air-gapped lab machine, first-run failure, CI).
The health endpoint reports which backend is live so this can never be mistaken
for working semantic search.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Sequence

import numpy as np

from ..config import get_settings
from ..logging_setup import get_logger
from ..store import cache_get, cache_put

log = get_logger("embeddings")

_model = None
_model_lock = threading.Lock()
_active_backend = "uninitialised"


def active_backend() -> str:
    return _active_backend


def _load_sentence_transformer():
    global _model, _active_backend
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        settings = get_settings()
        from sentence_transformers import SentenceTransformer

        log.info("loading_embedding_model", model=settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model)
        _active_backend = "sentence_transformers"
        return _model


def _hash_embed(text: str, dim: int) -> np.ndarray:
    """Hashed character-trigram projection. Deterministic, offline, weak."""
    vector = np.zeros(dim, dtype=np.float32)
    lowered = f"  {text.lower()}  "
    for size in (3, 4):
        for i in range(len(lowered) - size + 1):
            gram = lowered[i : i + size]
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


class Embedder:
    def __init__(self) -> None:
        global _active_backend
        settings = get_settings()
        self.dim = settings.embedding_dim
        self.backend = settings.embedding_backend
        self.model_name = settings.embedding_model
        if self.backend == "sentence_transformers":
            try:
                model = _load_sentence_transformer()
                self.dim = int(model.get_sentence_embedding_dimension())
            except Exception as exc:
                log.warning(
                    "embedding_model_unavailable_falling_back",
                    error=str(exc),
                    fallback="hash",
                    consequence="semantic recall is degraded; lexical search still works",
                )
                self.backend = "hash"
        if self.backend == "hash":
            # Never let the signature claim a model that is not in use: the
            # signature is part of the index version, and a stale index that
            # looks like a MiniLM index is worse than no index.
            self.model_name = f"hash-trigram-{self.dim}d"
            _active_backend = "hash"

    @property
    def signature(self) -> str:
        """Identifies the embedding space. Changing it invalidates the index."""
        return f"{self.backend}:{self.model_name}:{self.dim}"

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self.backend == "sentence_transformers":
            model = _load_sentence_transformer()
            vectors = model.encode(
                list(texts),
                batch_size=32,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return np.asarray(vectors, dtype=np.float32)
        return np.vstack([_hash_embed(t, self.dim) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        """Query embeddings are cached (D7): the same question recurs constantly
        across a claims book, and the agent itself re-issues near-identical
        queries on its second pass."""
        settings = get_settings()
        if not settings.cache_embeddings:
            return self.embed_documents([text])[0]

        digest = hashlib.sha256(f"{self.signature}|{text}".encode("utf-8")).hexdigest()[:40]
        cached = cache_get("embedding", digest)
        if cached is not None:
            return np.asarray(cached, dtype=np.float32)
        vector = self.embed_documents([text])[0]
        cache_put("embedding", digest, vector.tolist())
        return vector


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


def reset_embedder() -> None:
    global _embedder, _model
    _embedder = None
    _model = None
