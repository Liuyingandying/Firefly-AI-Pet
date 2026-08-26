"""MigrationOrchestrator: coordinate the three committers with dry-run/rollback/audit.

Stage 3 commit component. ``run(dry_run=True)`` performs no writes; ``run(dry_run=False)``
commits and records a bond snapshot for rollback. Every run writes an audit log.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..analyzer.candidates import MigrationCandidate
from .bond_replay_importer import BondReplayImporter
from .memory_committer import MemoryCommitter
from .style_profile_importer import StyleProfileImporter


class MigrationOrchestrator:
    """Run a migration (dry-run or commit) and produce an audit log."""

    def __init__(
        self,
        memory_committer: MemoryCommitter,
        bond_importer: BondReplayImporter,
        style_importer: StyleProfileImporter,
        *,
        bond_engine=None,
        restore_bond: Callable[[Any], None] | None = None,
        audit_path: str | Path | None = None,
    ) -> None:
        self.memory_committer = memory_committer
        self.bond_importer = bond_importer
        self.style_importer = style_importer
        self.bond_engine = bond_engine
        self.restore_bond = restore_bond
        self.audit_path = Path(audit_path) if audit_path is not None else None
        self._bond_snapshot = None

    def run(self, migration: MigrationCandidate, *, dry_run: bool = True) -> dict[str, Any]:
        run_id = migration.run_id or self._new_run_id()
        audit: dict[str, Any] = {
            "run_id": run_id,
            "status": "dry_run" if dry_run else "committed",
            "source": migration.source,
            "source_file": migration.source_file,
            "rule_version": migration.rule_version,
        }

        if dry_run:
            audit["memory"] = self.memory_committer.dry_run(migration.memory, run_id)
            audit["bond"] = self._bond_dry_run(migration.bond)
            audit["style"] = self.style_importer.dry_run(migration.style, run_id)
        else:
            if self.bond_engine is not None:
                self._bond_snapshot = self.bond_engine.read()
            audit["memory"] = self.memory_committer.commit(migration.memory, run_id)
            audit["bond"] = self._bond_commit(migration.bond)
            audit["style"] = self.style_importer.commit(migration.style, run_id)

        self._write_audit(audit)
        return audit

    def rollback(self, run_id: str) -> dict[str, Any]:
        audit: dict[str, Any] = {
            "run_id": run_id,
            "status": "rolled_back",
            "memory": self.memory_committer.rollback(run_id),
            "style": self.style_importer.rollback(run_id),
        }
        if self._bond_snapshot is not None and self.restore_bond is not None:
            self.restore_bond(self._bond_snapshot)
            audit["bond"] = "restored"
            self._bond_snapshot = None
        else:
            audit["bond"] = "skipped"
        self._write_audit(audit)
        return audit

    def _bond_dry_run(self, candidates) -> dict[str, Any]:
        if self.bond_engine is None:
            return {"skipped": True}
        before = self.bond_engine.read()
        after = self.bond_importer.dry_run(candidates, before)
        return {"before": before.to_dict(), "after": after.to_dict()}

    def _bond_commit(self, candidates) -> dict[str, Any]:
        if self.bond_engine is None:
            return {"skipped": True}
        after = self.bond_importer.commit(self.bond_engine, candidates)
        return {"after": after.to_dict()}

    def _write_audit(self, audit: dict[str, Any]) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _new_run_id() -> str:
        return f"migration-{int(time.time() * 1000)}"


__all__ = ["MigrationOrchestrator"]
