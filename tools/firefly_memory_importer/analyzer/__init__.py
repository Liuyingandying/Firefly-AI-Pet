"""Firefly history migration analyzer (Stage 1).

Pure Python; no ``core``/``memory``/``character`` imports and no LLM calls.
"""

from .candidates import (
    Band,
    BondCandidate,
    BondSignalType,
    Disposition,
    MemoryCandidate,
    MemoryCategory,
    MigrationCandidate,
    NarrativeCandidate,
    NarrativeType,
    SourceRef,
    StyleCandidate,
    StyleDimension,
)

__all__ = [
    "Band",
    "BondCandidate",
    "BondSignalType",
    "Disposition",
    "MemoryCandidate",
    "MemoryCategory",
    "MigrationCandidate",
    "NarrativeCandidate",
    "NarrativeType",
    "SourceRef",
    "StyleCandidate",
    "StyleDimension",
]
