"""Tests for MigrationOrchestrator (dry-run, commit, rollback, audit)."""

from __future__ import annotations

import json
from pathlib import Path

from core.bond_rules import apply_bond_rule
from core.bond_state import BondState

from tools.firefly_memory_importer.models import Role
from tools.firefly_memory_importer.analyzer.candidates import (
    Band,
    BondCandidate,
    BondSignalType,
    Disposition,
    MemoryCandidate,
    MemoryCategory,
    MigrationCandidate,
    SourceRef,
    StyleCandidate,
    StyleDimension,
)
from tools.firefly_memory_importer.committer.bond_replay_importer import (
    BondMigrationPolicy,
    BondReplayImporter,
)
from tools.firefly_memory_importer.committer.memory_committer import MemoryCommitter
from tools.firefly_memory_importer.committer.orchestrator import MigrationOrchestrator
from tools.firefly_memory_importer.committer.style_profile_importer import (
    StyleProfileImporter,
)


class FakeRepository:
    def __init__(self) -> None:
        self.records: dict = {}

    def add(self, record):
        self.records[record.id] = record
        return record.id

    def update(self, record_id, patch):
        record = self.records[record_id]
        self.records[record_id] = record.__class__(**{**record.to_dict(), **patch})
        return self.records[record_id]

    def delete(self, record_id):
        return self.records.pop(record_id, None) is not None

    def list(self):
        return list(self.records.values())


class FakeBondEngine:
    def __init__(self, state: BondState | None = None) -> None:
        self._state = state or BondState()

    def read(self) -> BondState:
        return self._state

    def apply(self, signal) -> BondState:
        transition = apply_bond_rule(self._state, signal)
        self._state = BondState(
            phase=transition.phase,
            trust_level=transition.trust_level,
            familiarity_level=transition.familiarity_level,
            shared_milestones=transition.shared_milestones,
            pending_promises=transition.pending_promises,
        )
        return self._state


def _ref() -> SourceRef:
    return SourceRef("c1", 0, Role.USER)


def _memory() -> MemoryCandidate:
    return MemoryCandidate(
        id="m-1",
        category=MemoryCategory.USER_FACT,
        content="用户是医生",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="mem.test",
        disposition=Disposition.AUTO_APPROVE,
    )


def _bond() -> BondCandidate:
    return BondCandidate(
        id="b-1",
        signal_type=BondSignalType.THANKED,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="bond.thanked",
        disposition=Disposition.AUTO_APPROVE,
    )


def _style() -> StyleCandidate:
    return StyleCandidate(
        id="s-1",
        dimension=StyleDimension.NICKNAME,
        content="小萤",
        confidence=0.8,
        band=Band.HIGH,
        evidence=(_ref(),),
        rule="style.nickname",
        disposition=Disposition.NEEDS_REVIEW,
    )


def _orchestrator(tmp_path: Path) -> tuple[MigrationOrchestrator, FakeRepository, FakeBondEngine]:
    repo = FakeRepository()
    memory = MemoryCommitter(repo)
    bond_importer = BondReplayImporter(BondMigrationPolicy(turn_signal_batch=1))
    style = StyleProfileImporter(memory, tmp_path / "style_profile.json")
    engine = FakeBondEngine()
    orchestrator = MigrationOrchestrator(
        memory,
        bond_importer,
        style,
        bond_engine=engine,
        restore_bond=lambda state: setattr(engine, "_state", state),
        audit_path=tmp_path / "audit.json",
    )
    return orchestrator, repo, engine


def _migration() -> MigrationCandidate:
    return MigrationCandidate(
        run_id="run-1",
        source_file="conversations.txt",
        source="doubao",
        rule_version=1,
        memory=[_memory()],
        bond=[_bond()],
        style=[_style()],
    )


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    orchestrator, repo, engine = _orchestrator(tmp_path)

    audit = orchestrator.run(_migration(), dry_run=True)

    assert audit["status"] == "dry_run"
    assert repo.records == {}
    assert engine.read().trust_level == 0.0  # bond unchanged
    assert not (tmp_path / "style_profile.json").exists()

    saved = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == "run-1"


def test_commit_writes_and_preserves_run_id(tmp_path: Path) -> None:
    orchestrator, repo, engine = _orchestrator(tmp_path)

    audit = orchestrator.run(_migration(), dry_run=False)

    assert audit["status"] == "committed"
    assert len(repo.records) == 2  # 1 memory + 1 style preference
    assert all(r.trigger == "migration:run-1" for r in repo.records.values())
    assert engine.read().trust_level == 0.02  # bond replayed


def test_rollback_restores_state(tmp_path: Path) -> None:
    orchestrator, repo, engine = _orchestrator(tmp_path)

    orchestrator.run(_migration(), dry_run=False)
    audit = orchestrator.rollback("run-1")

    assert audit["status"] == "rolled_back"
    assert repo.records == {}
    assert engine.read().trust_level == 0.0  # bond snapshot restored
    assert not (tmp_path / "style_profile.json").exists()
