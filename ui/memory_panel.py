"""Memory transparency panel: view Firefly's long-term information.

A self-contained Qt widget that reads (and, for memory, deletes) the three
long-term sources — MemoryService, Narrative records, and BondStateEngine —
without changing their write logic. All reads and deletes go through the
existing interfaces. The data formatting is a pure function so it can be tested
without Qt.

P5B upgrades: keyword search, category filter, detail panel, export, clear-all,
status bar, delete confirmation.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from memory.records import CATEGORY_DEFAULTS, MemoryCategory


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
                    "updated_ts": record.updated_ts,
                    "trigger": record.trigger,
                    "permission": _enum_value(record.permission),
                    "retention_half_life_days": record.retention_half_life_days,
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


def _format_ts(ms_ts: int) -> str:
    """Format a millisecond timestamp into a human-readable local time string."""
    try:
        dt = datetime.fromtimestamp(ms_ts / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (OSError, ValueError, OverflowError):
        return str(ms_ts)


class MemoryPanel(QWidget):
    """A read-only view of Memory, Narrative, and Bond state.

    P5B: keyword search, category filter, detail panel, export, clear-all,
    delete confirmation, status bar.
    """

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
        self._all_records: list[dict[str, Any]] = []
        self._selected_record_id: str | None = None

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        # --- Toolbar row ------------------------------------------------
        toolbar = QHBoxLayout()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索记忆…")
        self.search_input.textChanged.connect(self._on_search)
        toolbar.addWidget(QLabel("搜索:"))
        toolbar.addWidget(self.search_input, 1)

        toolbar.addWidget(QLabel("分类:"))
        self.category_filter = QComboBox()
        self.category_filter.addItem("全部", "all")
        for cat in MemoryCategory:
            self.category_filter.addItem(cat.value, cat.value)
        self.category_filter.currentIndexChanged.connect(self._on_search)
        toolbar.addWidget(self.category_filter)

        self.status_label = QLabel("就绪")
        toolbar.addWidget(self.status_label)
        main_layout.addLayout(toolbar)

        # --- Splitter: list + detail ------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: memory list
        left_layout = QVBoxLayout()
        self.memory_list = QListWidget()
        self.memory_list.currentItemChanged.connect(self._on_select)
        left_layout.addWidget(self.memory_list)
        left_buttons = QHBoxLayout()
        self.delete_button = QPushButton("删除选中")
        self.delete_button.clicked.connect(self._delete_selected)
        self.export_button = QPushButton("导出全部")
        self.export_button.clicked.connect(self._export_all)
        self.clear_button = QPushButton("清空全部记忆")
        self.clear_button.setStyleSheet("color: red;")
        self.clear_button.clicked.connect(self._clear_all)
        left_buttons.addWidget(self.delete_button)
        left_buttons.addWidget(self.export_button)
        left_buttons.addWidget(self.clear_button)
        left_layout.addLayout(left_buttons)
        splitter.addWidget(QWidget())
        splitter.widget(0).setLayout(left_layout)

        # Right: detail panel
        self.detail_panel = self._build_detail_panel()
        splitter.addWidget(self.detail_panel)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter)

        # --- Tabs for Narrative / Bond / Suggestions --------------------
        self.tabs = QTabWidget()
        self.narrative_list = QListWidget()
        self.bond_label = QLabel()
        self.tabs.addTab(self.memory_list, "Memory")
        self.tabs.addTab(self.narrative_list, "Narrative")
        self.tabs.addTab(self.bond_label, "Bond")
        main_layout.addWidget(self.tabs)

        if self.suggestion_service is not None:
            self._build_pending_tab()

    def _build_detail_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("<b>记忆详情</b>"))

        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumHeight(400)
        layout.addWidget(self.detail_text)
        return panel

    # --- Pending Suggestions tab ----------------------------------------
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

    # --- Actions --------------------------------------------------------
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

    def clear_all_memories(self) -> int:
        """Clear all long-term memory records via repository.

        Returns the number of records removed, or -1 on error.
        """
        try:
            return self.memory_service.repository.clear()
        except Exception:
            return -1

    def export_memories(self) -> dict[str, Any] | None:
        """Export all memory records as a JSON-serialisable dict."""
        try:
            records = self.memory_service.list()
            return {
                "format": "firefly-memory-export",
                "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "record_count": len(records),
                "records": [r.to_dict() for r in records],
            }
        except Exception:
            return None

    # --- Event handlers -------------------------------------------------
    def _on_search(self) -> None:
        """Filter the memory list by keyword and category."""
        keyword = self.search_input.text().strip().lower()
        category = self.category_filter.currentData()
        self._render_filtered(keyword, category)

    def _on_select(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None) -> None:
        """Show detail for the selected record."""
        if current is None:
            return
        record_id = current.data(Qt.ItemDataRole.UserRole)
        self._selected_record_id = record_id
        record = next(
            (r for r in self._all_records if r["id"] == record_id), None
        )
        if record is not None:
            self._render_detail(record)

    def _delete_selected(self) -> None:
        item = self.memory_list.currentItem()
        if item is None:
            return
        record_id = item.data(Qt.ItemDataRole.UserRole)
        record = next((r for r in self._all_records if r["id"] == record_id), None)
        label = record["content"][:60] if record else record_id
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除这条记忆吗？\n\n\"{label}\"\n\n此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self.delete_memory(record_id):
                self.refresh()
                self.status_label.setText(f"已删除 1 条记忆")
            else:
                self.status_label.setText("删除失败：记录不存在")

    def _export_all(self) -> None:
        data = self.export_memories()
        if data is None:
            QMessageBox.warning(self, "导出失败", "导出记忆时发生错误。")
            return
        text = json.dumps(data, ensure_ascii=False, indent=2)
        # Show in a dialog as a simple export path (file picker deferred to P5C)
        dlg = QMessageBox(self)
        dlg.setWindowTitle("导出记忆")
        dlg.setText(f"共 {data['record_count']} 条记忆\n\n点击 OK 复制 JSON 到剪贴板。")
        dlg.setInformativeText(text[:500] + ("…" if len(text) > 500 else ""))
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Ok
            | QMessageBox.StandardButton.Cancel
        )
        # Copy to clipboard if available
        try:
            from PySide6.QtGui import QClipboard
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            dlg.setInformativeText(f"已复制到剪贴板（{len(text)} 字符）\n共 {data['record_count']} 条记忆")
        except Exception:
            pass
        dlg.exec()
        self.status_label.setText(f"已导出 {data['record_count']} 条记忆")

    def _clear_all(self) -> None:
        reply = QMessageBox.question(
            self,
            "确认清空全部长期记忆",
            (
                "确定要清空所有长期记忆吗？\n\n"
                "这将删除所有 MemoryRecord。\n"
                "不会删除：角色身份、Bond 状态、聊天记录。\n\n"
                "此操作不可恢复。"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            removed = self.clear_all_memories()
            if removed >= 0:
                self.refresh()
                self.status_label.setText(f"已清空 {removed} 条记忆")
            else:
                self.status_label.setText("清空失败：发生错误")

    # --- Rendering ------------------------------------------------------
    def _render(self, data: dict[str, Any]) -> None:
        self._all_records = data["memory"]
        self.memory_list.clear()
        self._render_filtered("", "all")

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
        self.status_label.setText(f"共 {len(self._all_records)} 条记忆")

    def _render_filtered(self, keyword: str, category: str) -> None:
        self.memory_list.clear()
        for record in self._all_records:
            if category != "all" and record["category"] != category:
                continue
            if keyword and keyword not in record["content"].lower():
                continue
            item = QListWidgetItem(
                f"[{record['category']}] {record['content']} "
                f"(weight={record['weight']})"
            )
            item.setData(Qt.ItemDataRole.UserRole, record["id"])
            self.memory_list.addItem(item)

    def _render_detail(self, record: dict[str, Any]) -> None:
        self.detail_text.setPlainText(
            f"ID: {record['id']}\n"
            f"内容: {record['content']}\n"
            f"分类: {record['category']}\n"
            f"来源: {record['source']}\n"
            f"创建时间: {_format_ts(record['created_ts'])}\n"
            f"更新时间: {_format_ts(record.get('updated_ts', record['created_ts']))}\n"
            f"权重: {record['weight']}\n"
            f"保留半衰期: {record.get('retention_half_life_days', 'N/A')} 天\n"
            f"触发器: {record.get('trigger', 'N/A')}\n"
            f"权限: {record.get('permission', 'N/A')}"
        )

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


__all__ = ["MemoryPanel", "build_snapshot", "_format_ts"]
