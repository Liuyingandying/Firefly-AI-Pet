"""Deterministic re-ranking for Firefly memory search results.

This module is intentionally free of external LLM calls.  It composes four
multiplicative factors from existing MemoryRecord fields and CATEGORY_DEFAULTS
to produce a final relevance score that respects category importance, user-
assigned weight, and temporal freshness.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .records import CATEGORY_DEFAULTS, MemoryCategory, MemoryRecord


@dataclass(frozen=True, slots=True)
class RankedHit:
    """A single memory record annotated with ranking factors."""

    record: MemoryRecord
    semantic_score: float
    category_weight: float
    importance_factor: float
    temporal_decay: float
    final_score: float


def calculate_temporal_decay(
    record: MemoryRecord, reference_ts: int | None = None
) -> float:
    """Return 0.5 ** (age_days / half_life_days).

    Older memories decay faster when half_life is short (e.g. emotion).
    shared_experience / relationship have longer half-lives and decay slower.
    """
    if reference_ts is None:
        reference_ts = time.time_ns() // 1_000_000
    age_ms = reference_ts - record.created_ts
    age_days = age_ms / 86_400_000
    half_life = record.retention_half_life_days
    if half_life <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life)


def calculate_category_weight(category: MemoryCategory) -> float:
    """Look up the category weight from CATEGORY_DEFAULTS."""
    return CATEGORY_DEFAULTS[category]["weight"]


def calculate_importance_factor(weight: float) -> float:
    """Clamp weight to [0.0, 2.0] and return it directly as the importance factor."""
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        return 1.0
    clamped = max(0.0, min(2.0, float(weight)))
    return clamped


def calculate_final_score(
    semantic_score: float,
    record: MemoryRecord,
    reference_ts: int | None = None,
) -> float:
    """Product of all four ranking factors."""
    cat_weight = calculate_category_weight(record.category)
    importance = calculate_importance_factor(record.weight)
    decay = calculate_temporal_decay(record, reference_ts)
    return semantic_score * cat_weight * importance * decay


def re_rank_results(
    hits: list[MemoryRecord],
    semantic_scores: dict[str, float],
    reference_ts: int | None = None,
) -> list[RankedHit]:
    """Take a list of resolved MemoryRecords and their semantic scores,
    compute final scores, and return them sorted by final_score descending.

    Parameters
    ----------
    hits:
        Resolved MemoryRecord instances (already passed dedup / privacy guards).
    semantic_scores:
        Mapping from record.id -> semantic_score from Mem0.
    reference_ts:
        Optional reference timestamp (ms). Defaults to now.
    """
    ranked: list[RankedHit] = []
    for record in hits:
        sem_score = semantic_scores.get(record.id, 0.0)
        if not isinstance(sem_score, (int, float)) or sem_score < 0:
            sem_score = 0.0
        final = calculate_final_score(sem_score, record, reference_ts)
        ranked.append(
            RankedHit(
                record=record,
                semantic_score=float(sem_score),
                category_weight=calculate_category_weight(record.category),
                importance_factor=calculate_importance_factor(record.weight),
                temporal_decay=calculate_temporal_decay(record, reference_ts),
                final_score=final,
            )
        )
    ranked.sort(key=lambda r: r.final_score, reverse=True)
    return ranked
