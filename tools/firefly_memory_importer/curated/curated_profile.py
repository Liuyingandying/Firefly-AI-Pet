"""Curated profile: group selected candidates into a user-editable profile.

Builds the ``curated_migration.json`` structure with a human-editable
``profile`` (identity / preferences / goals / important_events) plus the
commit-ready selected candidate lists. The profile is derived from the selector
output and never auto-modifies any character YAML.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..analyzer.candidates import (
    BondCandidate,
    MemoryCandidate,
    MigrationCandidate,
    StyleCandidate,
)
from .curated_selector import select


def build_profile(
    selection: dict[str, Any],
    *,
    run_id: str,
    source: str,
    source_file: str,
    rule_version: int,
) -> dict[str, Any]:
    """Group a selector result into the curated profile + commit-ready lists."""
    selected = selection["selected"]
    profile: dict[str, list[dict[str, Any]]] = {
        "identity": [],
        "preferences": [],
        "goals": [],
        "important_events": [],
    }

    for item in selected["memory"]:
        bucket = _memory_bucket(item.get("category"))
        if bucket:
            profile[bucket].append(_entry(item))
    for item in selected["bond"]:
        bucket = _bond_bucket(item.get("signal_type"))
        if bucket:
            profile[bucket].append(_entry(item))
    for item in selected["style"]:
        profile["preferences"].append(_entry(item))

    return {
        "run_id": run_id,
        "source": source,
        "source_file": source_file,
        "rule_version": rule_version,
        "profile": profile,
        "memory": selected["memory"],
        "bond": selected["bond"],
        "style": selected["style"],
        "skipped": selection.get("skipped", []),
    }


def curate(
    preview: dict[str, Any],
    *,
    run_id: str,
    source: str,
    source_file: str,
    rule_version: int,
) -> dict[str, Any]:
    """Full curated flow: select from a preview and build the profile."""
    return build_profile(
        select(preview),
        run_id=run_id,
        source=source,
        source_file=source_file,
        rule_version=rule_version,
    )


def to_migration(curated: dict[str, Any]) -> MigrationCandidate:
    """Reconstruct a ``MigrationCandidate`` from a curated output for commit."""
    return MigrationCandidate(
        run_id=curated["run_id"],
        source_file=curated["source_file"],
        source=curated["source"],
        rule_version=curated["rule_version"],
        memory=[MemoryCandidate.from_dict(m) for m in curated.get("memory", [])],
        bond=[BondCandidate.from_dict(b) for b in curated.get("bond", [])],
        style=[StyleCandidate.from_dict(s) for s in curated.get("style", [])],
    )


def write(curated: dict[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(curated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _memory_bucket(category: str | None) -> str | None:
    return {
        "user_fact": "identity",
        "preference": "preferences",
        "project": "goals",
        "shared_experience": "important_events",
        "emotion": "important_events",
    }.get(category or "")


def _bond_bucket(signal_type: str | None) -> str | None:
    signal = signal_type or ""
    if signal == "shared_milestone":
        return "important_events"
    if signal.startswith("promise"):
        return "goals"
    return None


def _entry(item: dict[str, Any]) -> dict[str, Any]:
    content = item.get("content") or item.get("detail") or ""
    source = (
        item.get("category")
        or item.get("signal_type")
        or item.get("dimension")
        or item.get("kind")
    )
    return {"content": content, "source": source, "evidence": item.get("evidence", [])}


__all__ = ["build_profile", "curate", "load", "to_migration", "write"]
