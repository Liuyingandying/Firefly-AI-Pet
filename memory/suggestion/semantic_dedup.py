"""Semantic similarity abstraction for suggestion dedup.

Reuses the existing Mem0 / FastEmbed embedding infrastructure already present
in ``memory.mem0_adapter``.  This module does **not** write pending suggestions
into the real Mem0 index — it computes pairwise embeddings in-process and
compares cosine similarity.

Failure degradation:
- If the embedding model cannot be loaded, all comparison methods return
  ``None`` so the caller falls back to exact-normalisation dedup.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from memory.records import MemoryCategory, MemoryRecord
from memory.suggestion.memory_candidate_detector import MemorySuggestion

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal exceptions
# ---------------------------------------------------------------------------


class _EmbeddingUnavailable(Exception):
    """Raised when the embedding backend cannot be initialised."""


class _SemanticDedupError(Exception):
    """Raised when a semantic comparison fails unexpectedly."""


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


def _compute_embedding(text: str) -> list[float]:
    """Return a normalised embedding vector for *text*.

    Only uses FastEmbed (same model as Mem0Adapter: BAAI/bge-small-zh-v1.5).
    If the embedding library is unavailable, raises _EmbeddingUnavailable
    so the caller falls back to exact-normalisation dedup.

    We deliberately do NOT implement a secondary semantic algorithm (e.g.
    n-gram) as fallback: when the embedding backend is unavailable we
    prefer exact dedup over silently switching to a low-quality semantic
    comparator.
    """
    return _fastembed_embedding(text)


def _fastembed_embedding(text: str) -> list[float]:
    """Use the FastEmbed model directly (same model as Mem0Adapter)."""
    try:
        from fastembed import TextEmbedding  # type: ignore
    except ImportError:
        raise _EmbeddingUnavailable("fastembed not installed") from None

    # BAAI/bge-small-zh-v1.5 is the model Mem0Adapter uses.
    embedding_model = TextEmbedding(
        model_name="BAAI/bge-small-zh-v1.5",
    )
    embeddings = list(embedding_model.embed(text))
    vec = next(iter(embeddings), None)
    if vec is None:
        raise _EmbeddingUnavailable("fastembed returned empty embedding")

    # Normalise to unit vector
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        raise _EmbeddingUnavailable("fastembed returned zero vector")
    return [v / norm for v in vec]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        raise _SemanticDedupError(
            f"vector dimension mismatch: {len(a)} vs {len(b)}"
        )
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(v * v for v in a))
    norm_b = math.sqrt(sum(v * v for v in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# SemanticDedup class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SemanticMatch:
    """Result of a semantic duplicate check."""

    is_duplicate: bool
    matched_content: str | None = None


class SemanticDedup:
    """Layer-2 dedup: semantic similarity within same category.

    This class caches embeddings to avoid recomputing them for the same text.
    All comparison methods return ``None`` on failure so the caller can fall
    back to exact-normalisation dedup.
    """

    def __init__(self, threshold: float = 0.85) -> None:
        self.threshold = threshold
        self._embeddings: dict[str, list[float]] = {}
        self._cache_misses = 0

    def _get_embedding(self, text: str) -> list[float] | None:
        """Return cached or computed embedding, or None on failure."""
        if text in self._embeddings:
            return self._embeddings[text]
        try:
            vec = _compute_embedding(text)
            self._embeddings[text] = vec
            return vec
        except Exception as exc:
            logger.debug(
                "SemanticDedup embedding failed for '%s': %s",
                text[:50],
                exc,
            )
            self._cache_misses += 1
            return None

    def check_pending(
        self,
        candidate: MemorySuggestion,
        existing_pending: list[MemorySuggestion],
    ) -> _SemanticMatch | None:
        """Check if candidate is a semantic duplicate of any pending suggestion.

        Only compares within the same category.
        Returns ``_SemanticMatch`` with ``is_duplicate=True`` if found,
        or ``None`` on failure (caller should fall back to exact dedup).
        """
        if not existing_pending:
            return _SemanticMatch(is_duplicate=False)

        candidate_vec = self._get_embedding(candidate.content)
        if candidate_vec is None:
            return None  # fallback to exact dedup

        for existing in existing_pending:
            if existing.category != candidate.category:
                continue
            existing_vec = self._get_embedding(existing.content)
            if existing_vec is None:
                continue
            try:
                sim = _cosine_similarity(candidate_vec, existing_vec)
                if sim >= self.threshold:
                    return _SemanticMatch(
                        is_duplicate=True,
                        matched_content=existing.content,
                    )
            except _SemanticDedupError:
                continue

        return _SemanticMatch(is_duplicate=False)

    def check_existing_records(
        self,
        candidate: MemorySuggestion,
        existing_records: list[MemoryRecord],
    ) -> _SemanticMatch | None:
        """Check if candidate is a semantic duplicate of any saved MemoryRecord.

        Only compares within the same category.
        Returns ``_SemanticMatch`` with ``is_duplicate=True`` if found,
        or ``None`` on failure (caller should fall back to exact dedup).
        """
        if not existing_records:
            return _SemanticMatch(is_duplicate=False)

        candidate_vec = self._get_embedding(candidate.content)
        if candidate_vec is None:
            return None  # fallback to exact dedup

        for record in existing_records:
            if record.category != candidate.category:
                continue
            record_vec = self._get_embedding(record.content)
            if record_vec is None:
                continue
            try:
                sim = _cosine_similarity(candidate_vec, record_vec)
                if sim >= self.threshold:
                    return _SemanticMatch(
                        is_duplicate=True,
                        matched_content=record.content,
                    )
            except _SemanticDedupError:
                continue

        return _SemanticMatch(is_duplicate=False)


__all__ = ["SemanticDedup", "_SemanticMatch"]
