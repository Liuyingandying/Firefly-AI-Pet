"""Migration review: accept/reject/edit preview candidates -> reviewed_migration.json.

Reads the Stage 2 ``migration_preview.json``, applies human review decisions, and
emits ``reviewed_migration.json``. It never commits: no runtime data is written
here, and the full evidence chain is preserved on every accepted candidate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReviewDecision:
    """One human decision for a candidate."""

    candidate_id: str
    action: str  # "accept" | "reject" | "edit"
    edited_content: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ("accept", "reject", "edit"):
            raise ValueError(f"invalid review action: {self.action!r}")


_PREVIEW_ONLY_KEYS = frozenset({"risk", "preview", "review"})


def apply_review(
    preview: dict[str, Any], decisions: list[ReviewDecision]
) -> dict[str, Any]:
    """Apply review decisions and return the ``reviewed_migration.json`` structure.

    Candidates with no decision default to rejected (never auto-accepted).
    ``edit`` replaces the candidate's ``content`` (memory/style) or ``detail``
    (bond). The evidence chain is carried through unchanged.
    """
    decision_map = {d.candidate_id: d for d in decisions}
    accepted: dict[str, list[dict[str, Any]]] = {"memory": [], "bond": [], "style": []}
    rejected: list[dict[str, Any]] = []

    for group in ("memory", "bond", "style"):
        for item in preview.get("groups", {}).get(group, []):
            candidate_id = item.get("id")
            decision = decision_map.get(candidate_id)
            if decision is None or decision.action == "reject":
                rejected.append(
                    {
                        "id": candidate_id,
                        "kind": item.get("kind"),
                        "content": item.get("content") or item.get("detail"),
                    }
                )
                continue

            if decision.action == "edit" and decision.edited_content is not None:
                edited = dict(item)
                if "content" in edited:
                    edited["content"] = decision.edited_content
                elif "detail" in edited:
                    edited["detail"] = decision.edited_content
                item = edited

            clean = {k: v for k, v in item.items() if k not in _PREVIEW_ONLY_KEYS}
            accepted[group].append(clean)

    return {
        "run_id": preview.get("run_id"),
        "source": preview.get("source"),
        "source_file": preview.get("source_file"),
        "rule_version": preview.get("rule_version"),
        "memory": accepted["memory"],
        "bond": accepted["bond"],
        "style": accepted["style"],
        "rejected": rejected,
        "decisions": {d.candidate_id: d.action for d in decisions},
    }


def load_preview(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_reviewed(reviewed: dict[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


__all__ = ["ReviewDecision", "apply_review", "load_preview", "write_reviewed"]
