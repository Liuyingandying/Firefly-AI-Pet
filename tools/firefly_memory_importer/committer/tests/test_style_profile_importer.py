"""Tests for StyleProfileImporter."""

from __future__ import annotations

import json
from pathlib import Path

from tools.firefly_memory_importer.models import Role
from tools.firefly_memory_importer.analyzer.candidates import (
    Band,
    Disposition,
    SourceRef,
    StyleCandidate,
    StyleDimension,
)
from tools.firefly_memory_importer.committer.memory_committer import MemoryCommitter
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


def _style(content="小萤", dimension=StyleDimension.NICKNAME) -> StyleCandidate:
    return StyleCandidate(
        id="s-1",
        dimension=dimension,
        content=content,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(SourceRef("c1", 0, Role.USER),),
        rule="style.nickname",
        disposition=Disposition.NEEDS_REVIEW,
        review_only=True,
    )


def _importer(tmp_path: Path) -> tuple[StyleProfileImporter, FakeRepository]:
    repo = FakeRepository()
    memory = MemoryCommitter(repo)
    importer = StyleProfileImporter(memory, tmp_path / "style_profile.json")
    return importer, repo


def test_dry_run_previews_without_writing(tmp_path: Path) -> None:
    importer, repo = _importer(tmp_path)

    result = importer.dry_run([_style()], "run-1")

    assert len(result["preference_records"]) == 1
    assert result["preference_records"][0]["category"] == "preference"
    assert len(result["profile_entries"]) == 1
    assert repo.records == {}
    assert not (tmp_path / "style_profile.json").exists()


def test_commit_writes_preference_and_profile(tmp_path: Path) -> None:
    importer, repo = _importer(tmp_path)

    result = importer.commit([_style()], "run-1")

    assert len(result["record_ids"]) == 1
    assert len(repo.records) == 1
    record = list(repo.records.values())[0]
    assert record.category.value == "preference"
    assert record.trigger == "migration:run-1"

    profile = json.loads((tmp_path / "style_profile.json").read_text(encoding="utf-8"))
    assert profile["run_id"] == "run-1"
    assert profile["review_only"] is True
    assert profile["entries"][0]["dimension"] == "nickname"


def test_rollback_removes_preference_and_profile(tmp_path: Path) -> None:
    importer, repo = _importer(tmp_path)

    importer.commit([_style()], "run-1")
    result = importer.rollback("run-1")

    assert repo.records == {}
    assert result["profile_removed"] is True
    assert not (tmp_path / "style_profile.json").exists()
