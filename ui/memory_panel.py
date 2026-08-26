"""Memory transparency panel: view Firefly's long-term information.

A self-contained Qt widget that reads (and, for memory, deletes) the three
long-term sources — MemoryService, Narrative records, and BondStateEngine —
without changing their write logic. All reads and deletes go through the
existing interfaces. The data formatting is a pure function so it can be tested
without Qt.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


def build_snapshot(
    memory_service: Any,
    bond_state_engine: Any,
    narrative_provider: Any = None,
) -> dict[str, Any]:
    """Return a plain, JSON-friendly dict of the three long-term sources.

    Pure and defensive: a missing or failing source yields an empty/None block
    rather than raising.
    """
    memory: list[dict[str, Any]] = []
    try:
        for record in memory_service.list():
            memory.append(
                {
                    "id": record.id,
                    "content": record.content,
                    "category": _enum_value(record.category),
                    "source": _enum_value(record.source),
                    "created_ts": record.created_ts,
                    "weight": record.weight,
                }
            )
    except Exception:
        memory = []

    narrative: list[dict[str, Any]] = []
    if narrative_provider is not None:
        try:
            records = (
                narrative_provider()
                if callable(narrative_provider)
                else narrative_provider.records()
            )
        except Exception:
            records = []
        for record in records:
            narrative.append(
                {
                    "narrative_type": _enum_value(record.narrative_type),
                    "content": record.content,
                    "evidence": [_ref_dict(ref) for ref in (record.evidence or [])],
                }
            )

    bond: dict[str, Any] = {
        "phase": None,
        "trust": None,
        "familiarity": None,
        "milestones": [],
    }
    if bond_state_engine is not None:
        try:
            state = bond_state_engine.read()
        except Exception:
            state = None
        if state is not None:
            bond = {
                "phase": _enum_value(state.phase),
                "trust": state.trust_level,
                "familiarity": state.familiarity_level,
                "milestones": list(state.shared_milestones),
            }

    return {"memory": memory, "narrative": narrative, "bond": bond}


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _ref_dict(ref: Any) -> Any:
    return ref.to_dict() if hasattr(ref, "to_dict") else ref


class MemoryPanel(QWidget):
    """A read-only view of Memory, Narrative, and Bond state."""

    def __init__(
        self,
        memory_service: Any,
        bond_state_engine: Any,
        narrative_provider: Callable[[], list[Any]] | Any = None,
        suggestion_service: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.memory_service = memory_service
        self.bond_state_engine = bond_state_engine
        self.narrative_provider = narrative_provider
        self.suggestion_service = suggestion_service
        self._pending_suggestions: list[Any] = []

        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)

        self.delete_button = QPushButton("删除选中记忆")
        self.delete_button.clicked.connect(self._delete_selected)

        self.source_button = QPushButton("查看来源")
        self.source_button.clicked.connect(self._show_source)

        self.tabs = QTabWidget()
        self.memory_list = QListWidget()
        self.narrative_list = QListWidget()
        self.bond_label = QLabel()

        self.tabs.addTab(self.memory_list, "Memory")
        self.tabs.addTab(self.narrative_list, "Narrative")
        self.tabs.addTab(self.bond_label, "Bond")

        layout.addWidget(self.refresh_button)
        layout.addWidget(self.delete_button)
        layout.addWidget(self.source_button)
        layout.addWidget(self.tabs)

        if self.suggestion_service is not None:
            self._build_pending_tab()

    def snapshot(self) -> dict[str, Any]:
        """Return the current data snapshot (testable without rendering)."""
        return build_snapshot(
            self.memory_service, self.bond_state_engine, self.narrative_provider
        )

    def refresh(self) -> None:
        data = self.snapshot()
        self._render(data)
        self._render_pending()

    def delete_memory(self, record_id: str) -> bool:
        """Delete one memory record through the existing service interface."""
        return self.memory_service.delete(record_id)

    def _render(self, data: dict[str, Any]) -> None:
        self.memory_list.clear()
        for record in data["memory"]:
            item = QListWidgetItem(
                f"[{record['category']}] {record['content']} "
                f"(weight={record['weight']})"
            )
            item.setData(Qt.ItemDataRole.UserRole, record["id"])
            self.memory_list.addItem(item)

        self.narrative_list.clear()
        for record in data["narrative"]:
            evidence_count = len(record["evidence"])
            self.narrative_list.addItem(
                f"[{record['narrative_type']}] {record['content']} "
                f"(evidence={evidence_count})"
            )

        bond = data["bond"]
        if bond["phase"] is None:
            self.bond_label.setText("（无 Bond 状态）")
        else:
            milestones = "、".join(bond["milestones"]) or "无"
            self.bond_label.setText(
                f"phase={bond['phase']}\n"
                f"trust={bond['trust']}\n"
                f"familiarity={bond['familiarity']}\n"
                f"milestones={milestones}"
            )

    def _delete_selected(self) -> None:
        item = self.memory_list.currentItem()
        if item is None:
            return
        record_id = item.data(Qt.ItemDataRole.UserRole)
        if self.delete_memory(record_id):
            self.refresh()

    def _show_source(self) -> None:
        item = self.memory_list.currentItem()
        if item is None:
            return
        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = next(
            (r for r in self.snapshot()["memory"] if r["id"] == record_id), None
        )
        if record is None:
            return
        QMessageBox.information(
            self,
            "记忆来源",
            f"ID: {record['id']}\n"
            f"分类: {record['category']}\n"
            f"来源: {record['source']}\n"
            f"创建时间: {record['created_ts']}\n"
            f"权重: {record['weight']}",
        )

    def _build_pending_tab(self) -> None:
        self.pending_list = QListWidget()
        self.accept_button = QPushButton("接受")
        self.reject_button = QPushButton("拒绝")
        self.accept_button.clicked.connect(self._accept_selected)
        self.reject_button.clicked.connect(self._reject_selected)

        pending_widget = QWidget()
        pending_layout = QVBoxLayout(pending_widget)
        pending_layout.addWidget(self.pending_list)
        pending_layout.addWidget(self.accept_button)
        pending_layout.addWidget(self.reject_button)
        self.tabs.addTab(pending_widget, "待确认")

    def _render_pending(self) -> None:
        if self.suggestion_service is None:
            return
        self.pending_list.clear()
        self._pending_suggestions = list(self.suggestion_service.list_pending())
        for suggestion in self._pending_suggestions:
            self.pending_list.addItem(
                f"[{suggestion.category.value}] {suggestion.content} "
                f"(conf={suggestion.confidence})"
            )

    def _accept_selected(self) -> None:
        row = self.pending_list.currentRow()
        if row < 0 or row >= len(self._pending_suggestions):
            return
        suggestion = self._pending_suggestions[row]
        self.suggestion_service.accept(suggestion)
        self.refresh()

    def _reject_selected(self) -> None:
        row = self.pending_list.currentRow()
        if row < 0 or row >= len(self._pending_suggestions):
            return
        suggestion = self._pending_suggestions[row]
        self.suggestion_service.reject(suggestion)
        self.refresh()


__all__ = ["MemoryPanel", "build_snapshot"]
