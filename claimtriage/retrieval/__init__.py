"""Corpus ingestion and retrieval (L1, D2)."""

from .index import ClauseIndex, SearchHit, get_index, reset_index  # noqa: F401
from .embeddings import Embedder, get_embedder  # noqa: F401
