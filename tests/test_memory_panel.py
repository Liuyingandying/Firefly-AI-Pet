"""Offline tests for the Memory Transparency panel."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.memory_panel import MemoryPanel, build_snapshot


class _Enum:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeRecord:
    def __init__(self, id, content, category, source, created_ts, weight) -> None:
        self.id = id
        self.content = content
        self.category = _Enum(category)
        self.source = _Enum(source)
        self.created_ts = created_ts
        self.weight = weight


class FakeMemoryService:
    def __init__(self, records=None, fail: bool = False) -> None:
        self.records = list(records or [])
        self.fail = fail
        self.deleted: list[str] = []

    def list(self):
        if self.fail:
            raise RuntimeError("memory unavailable")
        return list(self.records)

    def delete(self, record_id: str) -> bool:
        self.deleted.append(record_id)
        for record in self.records:
            if record.id == record_id:
                self.records.remove(record)
                return True
        return False


class FakeBondState:
    def __init__(self, phase, trust, familiarity, milestones) -> None:
        self.phase = _Enum(phase)
        self.trust_level = trust
        self.familiarity_level = familiarity
        self.shared_milestones = milestones


class FakeBondEngine:
    def __init__(self, state=None, fail: bool = False) -> None:
        self.state = state
        self.fail = fail

    def read(self):
        if self.fail:
            raise OSError("bond unavailable")
        return self.state


class FakeNarrativeRecord:
    def __init__(self, narrative_type, content, evidence) -> None:
        self.narrative_type = _Enum(narrative_type)
        self.content = content
        self.evidence = evidence


def _record(id="m-1", content="用户喜欢猫", category="preference", source="migrated", created_ts=100, weight=0.9):
    return FakeRecord(id, content, category, source, created_ts, weight)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_snapshot_memory_records() -> None:
    memory = FakeMemoryService([_record()])

    snapshot = build_snapshot(memory, None)

    assert len(snapshot["memory"]) == 1
    record = snapshot["memory"][0]
    assert record["content"] == "用户喜欢猫"
    assert record["category"] == "preference"
    assert record["source"] == "migrated"
    assert record["created_ts"] == 100
    assert record["weight"] == 0.9


def test_snapshot_narrative_records() -> None:
    provider = lambda: [
        FakeNarrativeRecord("life_event", "毕业了", [{"conversation_id": "c1", "turn_seq": 0}])
    ]

    snapshot = build_snapshot(FakeMemoryService([]), None, provider)

    assert len(snapshot["narrative"]) == 1
    narrative = snapshot["narrative"][0]
    assert narrative["narrative_type"] == "life_event"
    assert narrative["content"] == "毕业了"
    assert narrative["evidence"] == [{"conversation_id": "c1", "turn_seq": 0}]


def test_snapshot_bond_state() -> None:
    bond = FakeBondEngine(FakeBondState("trusted", 0.7, 0.8, ["完成 v0.3"]))

    snapshot = build_snapshot(FakeMemoryService([]), bond)

    assert snapshot["bond"]["phase"] == "trusted"
    assert snapshot["bond"]["trust"] == 0.7
    assert snapshot["bond"]["familiarity"] == 0.8
    assert snapshot["bond"]["milestones"] == ["完成 v0.3"]


def test_snapshot_empty_and_none_sources() -> None:
    snapshot = build_snapshot(FakeMemoryService([]), None, None)

    assert snapshot["memory"] == []
    assert snapshot["narrative"] == []
    assert snapshot["bond"]["phase"] is None


def test_snapshot_source_failure_is_isolated() -> None:
    snapshot = build_snapshot(
        FakeMemoryService(fail=True),
        FakeBondEngine(FakeBondState("familiar", 0.5, 0.5, [])),
    )

    assert snapshot["memory"] == []  # memory failure -> empty
    assert snapshot["bond"]["phase"] == "familiar"  # bond unaffected


def test_panel_delete_memory_uses_service() -> None:
    _app()
    memory = FakeMemoryService([_record("m-1"), _record("m-2", content="用户是医生")])
    panel = MemoryPanel(memory, None)

    assert panel.delete_memory("m-1") is True
    assert "m-1" in memory.deleted
    assert [r.id for r in memory.records] == ["m-2"]


def test_panel_snapshot_matches_build_snapshot() -> None:
    _app()
    memory = FakeMemoryService([_record()])
    bond = FakeBondEngine(FakeBondState("familiar", 0.5, 0.5, []))
    panel = MemoryPanel(memory, bond)

    snapshot = panel.snapshot()

    assert snapshot == build_snapshot(memory, bond)
    assert len(snapshot["memory"]) == 1
    panel.close()


def test_panel_pending_tab_shows_suggestions() -> None:
    _app()
    from memory.suggestion.suggestion_service import SuggestionService

    class FakeRememberingService(FakeMemoryService):
        def remember(self, *args, **kwargs):
            return {"content": args[0]}

    memory = FakeRememberingService([])
    service = SuggestionService(memory)
    service.detect("我毕业了")

    panel = MemoryPanel(memory, None, suggestion_service=service)

    assert panel.tabs.count() == 4  # Memory / Narrative / Bond / 待确认
    assert len(panel._pending_suggestions) == 1
    assert panel._pending_suggestions[0].content == "毕业了"
    panel.close()
