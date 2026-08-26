"""Tests for Stage 3.8 zero-write migration validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.bond_rules import BondSignal, BondSignalType
from core.bond_state import BondState, BondStateEngine
from tools.firefly_memory_importer.validator.validation import (
    MigrationValidationError,
    validate_reviewed_migration,
)


def _reviewed() -> dict:
    evidence = [{"conversation_id": "c1", "turn_seq": 0, "role": "user"}]
    return {
        "run_id": "run-3-8",
        "source": "doubao",
        "source_file": "conversations.txt",
        "rule_version": 1,
        "memory": [
            {
                "id": "m-1",
                "kind": "memory",
                "category": "user_fact",
                "content": "用户是医生",
                "evidence": evidence,
            }
        ],
        "bond": [
            {
                "id": "b-1",
                "kind": "bond",
                "signal_type": "thanked",
                "detail": None,
                "evidence": evidence,
            }
        ],
        "style": [
            {
                "id": "s-1",
                "kind": "style",
                "dimension": "nickname",
                "content": "小萤",
                "rule": "style.nickname",
                "evidence": evidence,
            }
        ],
        "rejected": [{"id": "m-2", "kind": "memory", "content": "拒绝项"}],
        "decisions": {"m-1": "accept"},
    }


def test_dry_run_has_zero_repository_and_bond_writes(tmp_path: Path) -> None:
    reviewed_path = tmp_path / "reviewed_migration.json"
    reviewed_path.write_text(json.dumps(_reviewed()), encoding="utf-8")
    bond_path = tmp_path / "bond_state.json"
    real_engine = BondStateEngine(bond_path)
    before = real_engine.read()

    report = validate_reviewed_migration(reviewed_path, bond_state=before)

    assert report.safety_checks.repository_write_count == 0
    assert report.safety_checks.bond_persisted is False
    assert not bond_path.exists()
    assert real_engine.read() == before
    assert report.memory.predicted_records[0]["source"] == "migrated"
    assert report.style.predicted_profile["preference_records"][0]["category"] == "preference"


def test_bond_before_after_are_generated_by_engine_apply(monkeypatch) -> None:
    calls: list[BondSignal] = []
    original_apply = BondStateEngine.apply

    def observed_apply(self, signal):
        calls.append(signal)
        return original_apply(self, signal)

    monkeypatch.setattr(BondStateEngine, "apply", observed_apply)
    report = validate_reviewed_migration(_reviewed(), bond_state=BondState())

    assert report.bond.state_before["trust_level"] == 0.0
    assert report.bond.predicted_state_after["trust_level"] == 0.02
    assert report.bond.signal_summary == {
        "accepted_count": 1,
        "applied_count": 1,
        "by_type": {"thanked": 1},
    }
    assert calls == [BondSignal(BondSignalType.THANKED)]


def test_character_yaml_is_not_modified(tmp_path: Path) -> None:
    yaml_path = tmp_path / "identity.yaml"
    original = b"name: Firefly\n"
    yaml_path.write_bytes(original)

    report = validate_reviewed_migration(
        _reviewed(), bond_state=BondState(), character_yaml_paths=[yaml_path]
    )

    assert report.safety_checks.character_yaml_unchanged is True
    assert yaml_path.read_bytes() == original


@pytest.mark.parametrize(
    "missing",
    [
        "run_id",
        "source",
        "source_file",
        "rule_version",
        "memory",
        "bond",
        "style",
        "rejected",
        "decisions",
    ],
)
def test_missing_reviewed_field_fails_safely(missing: str) -> None:
    payload = _reviewed()
    del payload[missing]

    with pytest.raises(MigrationValidationError):
        validate_reviewed_migration(payload, bond_state=BondState(), character_yaml_paths=[])


def test_missing_candidate_field_fails_safely() -> None:
    payload = _reviewed()
    del payload["bond"][0]["signal_type"]

    with pytest.raises(MigrationValidationError):
        validate_reviewed_migration(payload, bond_state=BondState(), character_yaml_paths=[])


def test_report_json_serialization() -> None:
    report = validate_reviewed_migration(
        _reviewed(), bond_state=BondState(), character_yaml_paths=[]
    )

    restored = json.loads(report.to_json())
    assert restored["run_id"] == "run-3-8"
    assert restored["memory"]["accepted_count"] == 1
    assert restored["style"]["predicted_profile"]["review_only"] is True
    assert restored["safety_checks"]["rollback_available"] is True
