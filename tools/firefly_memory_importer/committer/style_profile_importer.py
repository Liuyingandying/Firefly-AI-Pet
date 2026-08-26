"""StyleProfileImporter: write style preferences + a review-only profile.

Stage 3 commit component. Accepted style candidates become ``preference``
MemoryRecords (via ``MemoryCommitter``) plus a review-only ``style_profile.json``
artifact. It never imports or modifies any ``character/*.yaml`` file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..analyzer.candidates import (
    Disposition,
    MemoryCandidate,
    MemoryCategory,
    StyleCandidate,
)
from .memory_committer import MemoryCommitter


def _preference_content(candidate: StyleCandidate) -> str:
    return f"{candidate.dimension.value}: {candidate.content}"


def _to_memory_candidate(candidate: StyleCandidate, run_id: str) -> MemoryCandidate:
    return MemoryCandidate(
        id=candidate.id,
        category=MemoryCategory.PREFERENCE,
        content=_preference_content(candidate),
        confidence=candidate.confidence,
        band=candidate.band,
        evidence=candidate.evidence,
        rule=candidate.rule,
        disposition=Disposition.NEEDS_REVIEW,
        flags=candidate.flags,
    )


class StyleProfileImporter:
    """Write style preferences as preference records plus a read-only profile."""

    def __init__(self, memory_committer: MemoryCommitter, style_profile_path: str | Path) -> None:
        self.memory_committer = memory_committer
        self.style_profile_path = Path(style_profile_path)

    def _profile_entries(self, candidates: list[StyleCandidate], run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "dimension": c.dimension.value,
                "content": c.content,
                "rule": c.rule,
                "evidence": [ref.to_dict() for ref in c.evidence],
                "review_only": True,
            }
            for c in candidates
        ]

    def dry_run(self, candidates: list[StyleCandidate], run_id: str) -> dict[str, Any]:
        """Preview the preference records and the profile without writing."""
        memory_candidates = [_to_memory_candidate(c, run_id) for c in candidates]
        return {
            "preference_records": self.memory_committer.dry_run(memory_candidates, run_id),
            "profile_entries": self._profile_entries(candidates, run_id),
        }

    def commit(self, candidates: list[StyleCandidate], run_id: str) -> dict[str, Any]:
        """Write preference records and the review-only style_profile.json."""
        memory_candidates = [_to_memory_candidate(c, run_id) for c in candidates]
        record_ids = self.memory_committer.commit(memory_candidates, run_id)
        self._write_profile(candidates, run_id)
        return {
            "record_ids": record_ids,
            "profile_path": str(self.style_profile_path),
        }

    def rollback(self, run_id: str) -> dict[str, Any]:
        """Delete preference records for run_id and remove the profile file."""
        deleted = self.memory_committer.rollback(run_id)
        removed = False
        try:
            self.style_profile_path.unlink(missing_ok=True)
            removed = True
        except OSError:
            removed = False
        return {"deleted_record_ids": deleted, "profile_removed": removed}

    def _write_profile(self, candidates: list[StyleCandidate], run_id: str) -> None:
        payload = {
            "run_id": run_id,
            "review_only": True,
            "entries": self._profile_entries(candidates, run_id),
        }
        self.style_profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.style_profile_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


__all__ = ["StyleProfileImporter"]
