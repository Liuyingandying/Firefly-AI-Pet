"""Narrative profile: group narrative candidates into a user-editable profile.

Builds ``narrative_profile.json`` with a human-editable ``profile`` grouped by
narrative type plus the full candidate list for review/conversion. The profile
is derived from extracted narrative and never writes Memory or Bond directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..analyzer.candidates import NarrativeCandidate

_NARRATIVE_TYPES = (
    "life_event",
    "emotional_turning_point",
    "shared_experience",
    "long_term_goal",
)


def build_profile(
    candidates: list[NarrativeCandidate],
    *,
    run_id: str,
    source: str,
    source_file: str,
    rule_version: int,
) -> dict[str, Any]:
    """Group candidates into the narrative profile plus commit-ready list."""
    profile: dict[str, list[dict[str, Any]]] = {t: [] for t in _NARRATIVE_TYPES}
    for candidate in candidates:
        bucket = candidate.narrative_type.value
        if bucket in profile:
            profile[bucket].append(_entry(candidate))

    return {
        "run_id": run_id,
        "source": source,
        "source_file": source_file,
        "rule_version": rule_version,
        "profile": profile,
        "candidates": [c.to_dict() for c in candidates],
    }


def to_candidates(profile: dict[str, Any]) -> list[NarrativeCandidate]:
    """Reconstruct candidate objects from a narrative profile for conversion."""
    return [
        NarrativeCandidate.from_dict(item) for item in profile.get("candidates", [])
    ]


def write(profile: dict[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _entry(candidate: NarrativeCandidate) -> dict[str, Any]:
    return {
        "content": candidate.content,
        "confidence": candidate.confidence,
        "band": candidate.band.value,
        "disposition": candidate.disposition.value,
        "evidence": [ref.to_dict() for ref in candidate.evidence],
    }


__all__ = ["build_profile", "load", "to_candidates", "write"]
