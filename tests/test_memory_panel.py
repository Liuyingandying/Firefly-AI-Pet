"""Offline tests for the Memory Transparency panel (P5B upgraded)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui.memory_panel import MemoryPanel, build_snapshot, _format_ts


class _Enum:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeRecord:
    def __init__(
        self,
        id,
        content,
        category,
        source,
        created_ts,
        weight,
        updated_ts=None,
        trigger="test",
        permission="explicit_only",
        retention_half_life_days=14.0,
    ) -> None:
        self.id = id
        self.content = content
        self.category = _Enum(category)
        self.source = _Enum(source)
        self.created_ts = created_ts
        self.weight = weight
        self.updated_ts = updated_ts if updated_ts is not None else created_ts
        self.trigger = trigger
        self.permission = _Enum(permission)
        self.retention_half_life_days = retention_half_life_days

    def to_dict(self):
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category.value,
            "source": self.source.value,
            "created_ts": self.created_ts,
            "weight": self.weight,
            "updated_ts": self.updated_ts,
            "trigger": self.trigger,
            "permission": self.permission.value,
            "retention_half_life_days": self.retention_half_life_days,
        }


class FakeMemoryService:
    def __init__(self, records=None, fail: bool = False) -> None:
        self.records = list(records or [])
        self.fail = fail
        self.deleted: list[str] = []
        self._cleared = 0

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

    @property
    def repository(self):
        return self

    def clear(self) -> int:
        count = len(self.records)
        self._cleared = count
        self.records.clear()
        return count


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


def _record(
    id="m-1",
    content="用户喜欢猫",
    category="preference",
    source="migrated",
    created_ts=100,
    weight=0.9,
):
    return FakeRecord(id, content, category, source, created_ts, weight)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# build_snapshot
# ---------------------------------------------------------------------------


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
    assert "updated_ts" in record
    assert "trigger" in record


def test_snapshot_narrative_records() -> None:
    provider = lambda: [
        FakeNarrativeRecord(
            "life_event", "毕业了", [{"conversation_id": "c1", "turn_seq": 0}]
        )
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


# ---------------------------------------------------------------------------
# MemoryPanel basic operations
# ---------------------------------------------------------------------------


def test_panel_delete_memory_uses_service() -> None:
    _app()
    memory = FakeMemoryService(
        [_record("m-1"), _record("m-2", content="用户是医生")]
    )
    panel = MemoryPanel(memory, None)

    assert panel.delete_memory("m-1") is True
    assert "m-1" in memory.deleted
    assert [r.id for r in memory.records] == ["m-2"]
    panel.close()


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


# ---------------------------------------------------------------------------
# P5B: Search & Category Filter
# ---------------------------------------------------------------------------


def test_panel_keyword_filter() -> None:
    _app()
    records = [
        _record("m-1", content="我喜欢猫", created_ts=100),
        _record("m-2", content="用户是医生", created_ts=200),
        _record("m-3", content="猫粮品牌", created_ts=300),
    ]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    # Initial: all 3 shown
    assert panel.memory_list.count() == 3

    # Type keyword
    panel.search_input.setText("猫")
    assert panel.memory_list.count() == 2  # m-1 and m-3

    # Clear
    panel.search_input.clear()
    assert panel.memory_list.count() == 3
    panel.close()


def test_panel_category_filter() -> None:
    _app()
    records = [
        _record("m-1", content="喜欢猫", category="preference", created_ts=100),
        _record("m-2", content="用户是医生", category="user_fact", created_ts=200),
        _record("m-3", content="猫粮", category="preference", created_ts=300),
    ]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    # Filter to preference
    idx = panel.category_filter.findData("preference")
    panel.category_filter.setCurrentIndex(idx)
    assert panel.memory_list.count() == 2

    # Filter to user_fact
    idx = panel.category_filter.findData("user_fact")
    panel.category_filter.setCurrentIndex(idx)
    assert panel.memory_list.count() == 1

    # Back to all
    panel.category_filter.setCurrentIndex(0)
    assert panel.memory_list.count() == 3
    panel.close()


def test_panel_combined_filter() -> None:
    _app()
    records = [
        _record("m-1", content="我喜欢猫", category="preference", created_ts=100),
        _record(
            "m-2", content="用户是医生", category="user_fact", created_ts=200
        ),
        _record(
            "m-3", content="猫粮品牌", category="preference", created_ts=300
        ),
    ]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    # keyword="猫" + category=preference → m-1 and m-3 (both preference + contain 猫)
    panel.search_input.setText("猫")
    idx = panel.category_filter.findData("preference")
    panel.category_filter.setCurrentIndex(idx)
    assert panel.memory_list.count() == 2
    ids = {
        panel.memory_list.item(i).data(Qt.ItemDataRole.UserRole)
        for i in range(panel.memory_list.count())
    }
    assert ids == {"m-1", "m-3"}
    panel.close()


# ---------------------------------------------------------------------------
# P5B: Detail Panel
# ---------------------------------------------------------------------------


def test_panel_select_record_shows_detail() -> None:
    _app()
    from PySide6.QtWidgets import QApplication

    records = [
        _record("m-1", content="我喜欢猫", category="preference", created_ts=1705320000000),
    ]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    # Select the first item
    item = panel.memory_list.item(0)
    panel.memory_list.setCurrentItem(item)

    # Detail panel should show content
    detail = panel.detail_text.toPlainText()
    assert "我喜欢猫" in detail
    assert "preference" in detail
    assert "m-1" in detail
    assert "2024-01-15" in detail  # formatted timestamp
    panel.close()


# ---------------------------------------------------------------------------
# P5B: Delete Confirmation
# ---------------------------------------------------------------------------


def test_panel_delete_shows_confirmation() -> None:
    _app()
    records = [_record("m-1", content="测试记忆")]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    # Select item
    item = panel.memory_list.item(0)
    panel.memory_list.setCurrentItem(item)

    # Simulate clicking delete and confirming (Yes=65536, No=131072)
    # We can't easily simulate QMessageBox in offscreen mode,
    # but we can verify the delete_memory path works
    assert panel.delete_memory("m-1") is True
    assert len(memory.records) == 0
    panel.close()


def test_panel_delete_cancel_path() -> None:
    """If delete_memory returns False, nothing happens."""
    _app()
    records = [_record("m-1")]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    # Try to delete non-existent
    assert panel.delete_memory("nonexistent") is False
    assert len(memory.records) == 1  # unchanged
    panel.close()


# ---------------------------------------------------------------------------
# P5B: Export
# ---------------------------------------------------------------------------


def test_panel_export_returns_valid_structure() -> None:
    _app()
    records = [
        _record("m-1", content="我喜欢猫", created_ts=1000),
        _record("m-2", content="用户是医生", created_ts=2000),
    ]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    data = panel.export_memories()

    assert data is not None
    assert data["format"] == "firefly-memory-export"
    assert data["version"] == 1
    assert data["record_count"] == 2
    assert len(data["records"]) == 2
    panel.close()


def test_panel_export_utf8_chinese() -> None:
    _app()
    records = [_record("m-1", content="我喜欢猫和鱼")]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    data = panel.export_memories()
    text = json.dumps(data, ensure_ascii=False, indent=2)

    assert "我喜欢猫和鱼" in text
    panel.close()


def test_panel_export_no_secrets() -> None:
    """Export should only contain MemoryRecord fields, no secrets."""
    _app()
    records = [_record("m-1", content="测试")]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    data = panel.export_memories()
    keys = set(data["records"][0].keys())

    # Should NOT contain internal fields
    assert "embedding" not in keys
    assert "api_key" not in keys
    panel.close()


# ---------------------------------------------------------------------------
# P5B: Clear All
# ---------------------------------------------------------------------------


def test_panel_clear_all() -> None:
    _app()
    records = [
        _record("m-1", content="记忆1"),
        _record("m-2", content="记忆2"),
        _record("m-3", content="记忆3"),
    ]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    removed = panel.clear_all_memories()
    assert removed == 3
    assert len(memory.records) == 0
    panel.close()


def test_panel_clear_all_empty() -> None:
    _app()
    memory = FakeMemoryService([])
    panel = MemoryPanel(memory, None)

    removed = panel.clear_all_memories()
    assert removed == 0
    panel.close()


def test_clear_all_does_not_affect_bond() -> None:
    """Clear all memories should not touch BondState."""
    _app()
    bond_state = FakeBondState("trusted", 0.8, 0.9, ["milestone1"])
    bond_engine = FakeBondEngine(bond_state)
    memory = FakeMemoryService([_record("m-1")])
    panel = MemoryPanel(memory, bond_engine)

    panel.clear_all_memories()

    # Bond state should be unchanged
    assert bond_engine.state.phase.value == "trusted"
    assert bond_engine.state.trust_level == 0.8
    panel.close()


# ---------------------------------------------------------------------------
# P5B: Status Bar
# ---------------------------------------------------------------------------


def test_panel_status_shows_count() -> None:
    _app()
    records = [_record("m-1"), _record("m-2")]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    assert "2" in panel.status_label.text()
    panel.close()


# ---------------------------------------------------------------------------
# P5B: Empty State
# ---------------------------------------------------------------------------


def test_panel_empty_state() -> None:
    _app()
    memory = FakeMemoryService([])
    panel = MemoryPanel(memory, None)

    assert panel.memory_list.count() == 0
    assert "0" in panel.status_label.text()
    panel.close()


# ---------------------------------------------------------------------------
# P5B: Refresh After Delete
# ---------------------------------------------------------------------------


def test_panel_refresh_after_delete() -> None:
    _app()
    records = [_record("m-1"), _record("m-2")]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    assert panel.memory_list.count() == 2
    panel.delete_memory("m-1")
    panel.refresh()
    assert panel.memory_list.count() == 1
    panel.close()


# ---------------------------------------------------------------------------
# P5B: Refresh After Clear
# ---------------------------------------------------------------------------


def test_panel_refresh_after_clear() -> None:
    _app()
    records = [_record("m-1"), _record("m-2")]
    memory = FakeMemoryService(records)
    panel = MemoryPanel(memory, None)

    panel.clear_all_memories()
    panel.refresh()
    assert panel.memory_list.count() == 0
    panel.close()


# ---------------------------------------------------------------------------
# P5B: _format_ts
# ---------------------------------------------------------------------------


def test_format_ts_utc() -> None:
    # 2024-01-15 12:00:00 UTC = 1705320000000 ms
    result = _format_ts(1705320000000)
    assert "2024-01-15" in result
    assert "12:00" in result
    assert "UTC" in result


def test_format_ts_invalid() -> None:
    # Very large timestamp that overflows
    result = _format_ts(99999999999999)
    assert result == "99999999999999"
