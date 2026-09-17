"""Memory History Restore (M3B.8) — restore physically-deleted superseded
history records from a known-good backup into the live Repository.

Boundary:
- Only records with ``lifecycle_status == "superseded"`` are restored.
- Active records are NEVER added, modified, or re-activated.
- Restore rules (in order):
    1. record_id already in current repository  -> SKIP   (id_exists)
    2. backup record is not superseded          -> CONFLICT
       (only superseded history is restorable; an absent ACTIVE record
        would change the active set and requires a human decision)
    3. content hash equals an existing ACTIVE record's content hash
       -> expected for superseded history (dedup chains) -> ADD with audit
       note ``content_matches_active``
    4. otherwise -> ADD.
- ``created_at`` / ``updated_at`` / ``superseded_by`` / ``supersede_reason``
  are preserved verbatim; lifecycle_status stays ``superseded``.
- After restore: every ``superseded_by`` must resolve to an existing
  record (chain integrity check).
- The repository is the source of truth: the semantic index is repaired
  through the standard consistency_report -> reconcile flow, never written
  directly by this module.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .repository import JsonMemoryRepository

RESTORABLE_LIFECYCLE = "superseded"


def _content_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(
        str(record.get("content", "")).encode("utf-8")
    ).hexdigest()


def load_backup_records(path: Path | str) -> list[dict[str, Any]]:
    """Parse a backup memory_records.json into raw record dicts."""
    data = json.loads(Path(path).read_bytes().decode("utf-8"))
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError(f"backup {path} has no records list")
    return [r for r in records if isinstance(r, dict)]


@dataclass
class RestorePlan:
    add_records: list[dict[str, Any]] = field(default_factory=list)
    skip_records: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "add_records": list(self.add_records),
            "skip_records": list(self.skip_records),
            "conflicts": list(self.conflicts),
        }


def plan_restore(
    backup_records: list[dict[str, Any]],
    repository: JsonMemoryRepository,
) -> RestorePlan:
    """Classify each backup record against the CURRENT repository.

    Rules (first match wins):
    - id present in current            -> skip (id_exists)
    - lifecycle is not "superseded"    -> conflict (active_set_change risk:
      absent ACTIVE records must be re-confirmed by a human, never auto-added)
    - content hash equals an existing ACTIVE record's content
      -> ADD as superseded history with audit note
        ("content_duplicate_of_active_expected")
    - otherwise                        -> ADD.
    """
    current_by_id = {r.id: r for r in repository.list()}
    active_content_hashes = {
        _content_hash(record.to_dict())
        for record in current_by_id.values()
        if getattr(record, "lifecycle_status", "active") == "active"
    }
    plan = RestorePlan()
    for record in backup_records:
        rid = str(record.get("id", ""))
        if not rid:
            plan.conflicts.append(
                {"record": record, "reason": "missing_id"}
            )
            continue
        if rid in current_by_id:
            plan.skip_records.append(
                {"id": rid, "reason": "id_exists_in_current"}
            )
            continue
        lifecycle = str(record.get("lifecycle_status", "active"))
        if lifecycle != RESTORABLE_LIFECYCLE:
            plan.conflicts.append(
                {
                    "id": rid,
                    "reason": (
                        f"not_restorable_lifecycle:{lifecycle} "
                        "(only superseded history auto-restores; absent "
                        "active records need human confirmation)"
                    ),
                }
            )
            continue
        entry = dict(record)
        if _content_hash(record) in active_content_hashes:
            # Superseded history duplicating canonical content is the
            # normal post-dedup shape — audited, not treated as a conflict.
            entry["restore_audit"] = "content_matches_active_expected"
        else:
            entry["restore_audit"] = "superseded_history"
        plan.add_records.append(entry)
    return plan


def apply_restore(
    plan: RestorePlan,
    repository: JsonMemoryRepository,
) -> dict[str, Any]:
    """Add every planned record via the Repository (source of truth).

    Returns counters.  Existing records are never touched.
    """
    from .records import MemoryRecord

    added = 0
    seen: set[str] = set()
    for entry in plan.add_records:
        payload = {
            key: value
            for key, value in entry.items()
            if key != "restore_audit"
        }
        record = MemoryRecord.from_dict(payload)
        if record.id in seen:
            plan.conflicts.append(
                {"id": record.id, "reason": "duplicate_in_plan"}
            )
            continue
        seen.add(record.id)
        repository.add(record)
        added += 1
    return {"added": added}


def verify_chains(repository: JsonMemoryRepository) -> tuple[bool, list[str]]:
    """Every superseded record's superseded_by must point to an existing id."""
    records = repository.list()
    by_id = {r.id for r in records}
    broken: list[str] = []
    for record in records:
        if getattr(record, "lifecycle_status", "active") != "superseded":
            continue
        target = getattr(record, "superseded_by", None)
        if target and target not in by_id:
            broken.append(record.id)
    return (len(broken) == 0, broken)
