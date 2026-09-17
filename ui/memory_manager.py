"""Memory Manager 2.0 — user-facing long-term memory management window.

Replaces the old debug-style MemoryPanel. Only manages Long-term Memory;
Bond / Narrative / Conversation / Learning are explicitly out of scope.

Data flows exclusively through MemoryService (never Repository directly for
mutations). M2A/M2C policies apply to all writes.
"""

from __future__ import annotations

import json
from pathlib import Path
import os
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui import theme

CATEGORY_LABELS: dict[str, str] = {
    "user_fact": "个人信息",
    "preference": "偏好",
    "project": "项目",
    "relationship": "关系",
    "shared_experience": "经历",
    "emotion": "情感",
}

_SOURCE_LABELS: dict[str, str] = {
    "explicit": "用户明确记住",
    "suggested": "对话中提取",
    "migrated": "导入",
}


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _fmt_ts_full(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


class MemoryManagerWindow(QWidget):
    """User-facing Memory Manager — active/history/filter/add/edit/forget."""

    memory_changed = Signal()

    def __init__(self, memory_service: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = self._confirmed_handle(memory_service)
        self.setWindowTitle("记忆管理")
        self.resize(900, 620)
        self._build_ui()
        self.refresh()

    @staticmethod
    def _confirmed_handle(service: Any) -> Any:
        """Derive a CONFIRMED_WRITE view for the manager's own actions.

        The manager is the explicit confirmation surface (edit / forget /
        clear all are user-confirmed in the UI), so it needs a
        CONFIRMED_WRITE handle (M3B.1).  Viewing stays read-only regardless —
        reads never write.  When the injected service already is
        CONFIRMED_WRITE it is reused as-is; otherwise a sibling handle over
        the SAME repository + semantic adapter is derived — never a second
        source of truth.
        """
        from memory.access_mode import MemoryAccessMode
        from memory.service import MemoryService

        mode = getattr(service, "access_mode", None)
        if isinstance(mode, MemoryAccessMode) and mode is MemoryAccessMode.CONFIRMED_WRITE:
            return service
        return MemoryService(
            service.repository,
            service.adapter,
            write_policy=service.write_policy,
            search_top_k=service.search_top_k,
            search_threshold=service.search_threshold,
            security_guard=service.security_guard,
            dedup_enabled=service.dedup_enabled,
            dedup_similarity_threshold=service.dedup_similarity_threshold,
            access_mode=MemoryAccessMode.CONFIRMED_WRITE,
        )

    # ------------------------------------------------------------ build UI

    def _build_ui(self) -> None:
        from .theme import V2, css_color

        self.setStyleSheet(
            f"MemoryManagerWindow {{ background: rgba{V2.BACKGROUND}; }}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # -- Header ---------------------------------------------------------
        title = QLabel("记忆管理")
        title.setStyleSheet(
            f"color: rgba{V2.TEXT_MAIN}; font-family: {theme.V2_FONT_STACK};"
            f"font-size: {theme.V2.FONT_TITLE}pt; font-weight: 700; background: transparent;"
        )
        root.addWidget(title)

        # -- Filter / search row --------------------------------------------
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索记忆…")
        self._search.textChanged.connect(self._refresh_list)
        filter_row.addWidget(self._search, 2)

        self._status_filter = QComboBox()
        for label, data in (("当前记忆", "active"), ("历史记忆", "superseded"),
                            ("全部", "all")):
            self._status_filter.addItem(label, data)
        self._status_filter.setCurrentIndex(0)
        self._status_filter.currentIndexChanged.connect(self._refresh_list)
        filter_row.addWidget(self._status_filter, 1)

        self._type_filter = QComboBox()
        self._type_filter.addItem("全部类型", "all")
        for cat_zh in CATEGORY_LABELS.values():
            self._type_filter.addItem(cat_zh, cat_zh)
        self._type_filter.currentIndexChanged.connect(self._refresh_list)
        filter_row.addWidget(self._type_filter, 1)

        add_btn = QPushButton("+ 添加记忆")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setStyleSheet(
            f"QPushButton {{ color: rgba{V2.TEXT_MAIN}; background: rgba{V2.CARD_BG_USER};"
            f" border: 1px solid rgba{V2.BORDER_SOFT}; border-radius: 10px;"
            f" padding: 4px 12px; font-family: {theme.V2_FONT_STACK};"
            f" font-size: {theme.V2.FONT_CAPTION}pt; }}"
            f"QPushButton:hover {{ border: 1px solid rgba{V2.PRIMARY_BLUE}; }}"
        )
        add_btn.clicked.connect(self._on_add)
        filter_row.addWidget(add_btn)

        root.addLayout(filter_row)

        # -- Statistics ------------------------------------------------------
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet(
            f"color: rgba{V2.TEXT_SECONDARY}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        root.addWidget(self._stats_label)

        # -- Splitter: list + detail ------------------------------------------
        from PySide6.QtWidgets import QSplitter, QListWidget, QListWidgetItem

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        # Left: memory list
        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget {{ background: rgba{V2.CARD_BG};"
            f" border: 1px solid rgba{V2.BORDER_SOFT}; border-radius: 14px;"
            f" font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
            f" color: rgba{V2.TEXT_MAIN}; }}"
            f"QListWidget::item {{ padding: 8px 12px; border-bottom: 1px solid rgba{V2.BORDER_SOFT}; }}"
            f"QListWidget::item:selected {{ background: rgba{V2.CARD_BG_USER}; }}"
        )
        self._list.currentItemChanged.connect(self._on_select)
        splitter.addWidget(self._list)

        # Right: detail panel
        detail_frame = QFrame()
        detail_frame.setStyleSheet(
            f"QFrame {{ background: rgba{V2.CARD_BG};"
            f" border: 1px solid rgba{V2.BORDER_SOFT}; border-radius: 14px; }}"
        )
        detail_layout = QVBoxLayout(detail_frame)
        detail_layout.setContentsMargins(16, 12, 16, 12)
        detail_layout.setSpacing(8)

        self._detail_title = QLabel("记忆详情")
        self._detail_title.setStyleSheet(
            f"color: rgba{V2.TEXT_MAIN}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_HEADING}pt; font-weight: 700;"
        )
        detail_layout.addWidget(self._detail_title)

        self._detail_content = QTextEdit()
        self._detail_content.setReadOnly(True)
        self._detail_content.setStyleSheet(
            f"QTextEdit {{ background: transparent; border: none;"
            f" font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
            f" color: rgba{V2.TEXT_MAIN}; }}"
        )
        detail_layout.addWidget(self._detail_content, 1)

        self._detail_info = QLabel("")
        self._detail_info.setWordWrap(True)
        self._detail_info.setStyleSheet(
            f"color: rgba{V2.TEXT_SECONDARY}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        detail_layout.addWidget(self._detail_info)

        # Edit / Forget buttons
        action_row = QHBoxLayout()
        edit_btn = QPushButton("编辑")
        edit_btn.setCursor(Qt.PointingHandCursor)
        edit_btn.setStyleSheet(
            f"QPushButton {{ color: rgba{V2.TEXT_MAIN}; background: rgba{V2.CARD_BG_USER};"
            f" border: 1px solid rgba{V2.BORDER_SOFT}; border-radius: 10px;"
            f" padding: 4px 12px; font-family: {theme.V2_FONT_STACK};"
            f" font-size: {theme.V2.FONT_CAPTION}pt; }}"
            f"QPushButton:hover {{ border: 1px solid rgba{V2.PRIMARY_BLUE}; }}"
        )
        edit_btn.clicked.connect(self._on_edit)
        action_row.addWidget(edit_btn)

        forget_btn = QPushButton("忘记")
        forget_btn.setCursor(Qt.PointingHandCursor)
        forget_btn.setStyleSheet(
            f"QPushButton {{ color: rgba{V2.TEXT_SECONDARY}; background: transparent;"
            f" border: 1px solid rgba{V2.BORDER_SOFT}; border-radius: 10px;"
            f" padding: 4px 12px; font-family: {theme.V2_FONT_STACK};"
            f" font-size: {theme.V2.FONT_CAPTION}pt; }}"
            f"QPushButton:hover {{ color: rgba(200, 80, 60, 255); }}"
        )
        forget_btn.clicked.connect(self._on_forget)
        action_row.addWidget(forget_btn)
        action_row.addStretch(1)
        detail_layout.addLayout(action_row)

        splitter.addWidget(detail_frame)
        root.addWidget(splitter, 1)

        # -- Bottom: export + advanced ----------------------------------------
        bottom = QHBoxLayout()
        export_btn = QPushButton("导出")
        export_btn.setCursor(Qt.PointingHandCursor)
        export_btn.setStyleSheet(
            f"QPushButton {{ color: rgba{V2.TEXT_SECONDARY}; background: transparent;"
            f" border: 1px solid rgba{V2.BORDER_SOFT}; border-radius: 10px;"
            f" padding: 4px 12px; font-family: {theme.V2_FONT_STACK};"
            f" font-size: {theme.V2.FONT_CAPTION}pt; }}"
            f"QPushButton:hover {{ color: rgba{V2.TEXT_MAIN}; }}"
        )
        export_btn.clicked.connect(self._on_export)
        bottom.addWidget(export_btn)

        advanced_btn = QPushButton("高级维护…")
        advanced_btn.setCursor(Qt.PointingHandCursor)
        advanced_btn.setStyleSheet(
            f"QPushButton {{ color: rgba{V2.TEXT_SECONDARY}; background: transparent;"
            f" border: none; font-family: {theme.V2_FONT_STACK};"
            f" font-size: {theme.V2.FONT_CAPTION}pt; }}"
            f"QPushButton:hover {{ color: rgba{V2.TEXT_MAIN}; }}"
        )
        advanced_btn.clicked.connect(self._on_advanced)
        bottom.addWidget(advanced_btn)
        bottom.addStretch(1)
        root.addLayout(bottom)

    # ------------------------------------------------------------- helpers

    def _all_records(self) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "id": r.id,
                    "content": r.content,
                    "category": r.category.value if hasattr(r.category, "value") else str(r.category),
                    "source": r.source.value if hasattr(r.source, "value") else str(r.source),
                    "created_ts": r.created_ts,
                    "updated_ts": r.updated_ts,
                    "lifecycle_status": getattr(r, "lifecycle_status", "active"),
                    "superseded_by": getattr(r, "superseded_by", None),
                }
                for r in self._service.list()
            ]
        except Exception:
            return []

    def _refresh_list(self) -> None:
        from PySide6.QtWidgets import QListWidgetItem

        self._list.clear()
        records = self._all_records()
        status = self._status_filter.currentData()
        type_zh = self._type_filter.currentData()
        keyword = self._search.text().strip().lower()

        cat_by_zh = {v: k for k, v in CATEGORY_LABELS.items()}
        filter_cat = cat_by_zh.get(type_zh) if type_zh != "all" else None

        shown = 0
        for r in records:
            lc = r.get("lifecycle_status", "active")
            if status == "active" and lc != "active":
                continue
            if status == "superseded" and lc != "superseded":
                continue
            cat_zh = CATEGORY_LABELS.get(r.get("category", ""), r.get("category", ""))
            if filter_cat and cat_zh != type_zh:
                continue
            if keyword and keyword not in (r.get("content", "") or "").lower():
                continue

            is_active = lc == "active"
            prefix = "当前" if is_active else "历史"
            cat_label = CATEGORY_LABELS.get(r.get("category", ""), "")
            date = _fmt_ts(r.get("created_ts"))
            line = f"{cat_label}  ·  {prefix}"
            if date:
                line += f"  ·  {date}"
            item = QListWidgetItem(line)
            item.setData(Qt.ItemDataRole.UserRole, r["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, r.get("content", ""))
            if not is_active:
                item.setForeground(Qt.gray)
            self._list.addItem(item)
            shown += 1

        active_n = sum(1 for r in records if r.get("lifecycle_status", "active") == "active")
        superseded_n = len(records) - active_n
        self._stats_label.setText(
            f"{active_n} 条当前 · {superseded_n} 条历史  |  当前视图: {shown}"
        )

        if shown == 0:
            self._list.addItem("（无匹配记录）")

    def refresh(self) -> None:
        """Public refresh: reload from service and re-render the list."""
        self._refresh_list()

    # ------------------------------------------------------------- actions

    def _on_select(self, current, _previous=None) -> None:
        if current is None:
            return
        record_id = current.data(Qt.ItemDataRole.UserRole)
        if not record_id or record_id.startswith("（"):
            return
        records = self._all_records()
        record = next((r for r in records if r["id"] == record_id), None)
        if record is None:
            return

        self._detail_content.setPlainText(record.get("content", ""))

        cat_zh = CATEGORY_LABELS.get(record.get("category", ""), record.get("category", ""))
        lc = record.get("lifecycle_status", "active")
        status_zh = "当前" if lc == "active" else "历史"
        source_zh = _SOURCE_LABELS.get(record.get("source", ""), record.get("source", ""))
        created = _fmt_ts_full(record.get("created_ts"))
        updated = _fmt_ts_full(record.get("updated_ts"))

        info_parts = [f"类型: {cat_zh}", f"状态: {status_zh}"]
        if source_zh:
            info_parts.append(f"来源: {source_zh}")
        if created:
            info_parts.append(f"创建: {created}")
        if updated and updated != created:
            info_parts.append(f"更新: {updated}")

        if lc == "superseded" and record.get("superseded_by"):
            successor = self._service.resolve_active_successor(record_id)
            if successor is not None:
                info_parts.append("后续版本: 已被更新")
        info = "\n".join(info_parts)
        self._detail_info.setText(info)

    def _on_add(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getMultiLineText(
            self, "添加记忆", "记忆内容：", ""
        )
        if not ok or not text.strip():
            return
        try:
            result = self._service.remember_detailed(
                text.strip(), asserted_explicit=True
            )
        except Exception as exc:  # noqa: BLE001
            self._stats_label.setText(f"添加失败: {exc}")
            return
        if result is None:
            self._stats_label.setText("未能记住这条内容。")
            return
        outcome = result.outcome
        if outcome.value == "exact_duplicate":
            self._stats_label.setText("这条记忆已经存在。")
        else:
            self._stats_label.setText("已记住")
        self._refresh_list()

    def _on_edit(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        item = self._list.currentItem()
        if item is None:
            return
        record_id = item.data(Qt.ItemDataRole.UserRole)
        if not record_id:
            return
        record = next(
            (r for r in self._all_records() if r["id"] == record_id), None
        )
        if record is None:
            return
        old_content = record.get("content", "")
        new_text, ok = QInputDialog.getMultiLineText(
            self, "编辑记忆", "修改记忆内容：", old_content
        )
        if not ok or not new_text.strip() or new_text.strip() == old_content.strip():
            return
        # M3A.1: unified edit through MemoryService.edit_memory()
        try:
            result = self._service.edit_memory(record_id, new_text.strip())
        except Exception as exc:
            self._stats_label.setText(f"编辑失败: {exc}")
            return
        if result.get("action") == "duplicate":
            self._stats_label.setText(result.get("message", "这条记忆已经存在。"))
            return
        self._stats_label.setText("记忆已更新")
        self._refresh_list()

    def _on_forget(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        record_id = item.data(Qt.ItemDataRole.UserRole)
        if not record_id:
            return
        reply = QMessageBox.question(
            self, "确认忘记",
            "确定让流萤忘记这条记忆吗？\n\n此操作会删除这条长期记忆。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.delete(record_id)
        except Exception as exc:
            self._stats_label.setText(f"删除失败: {exc}")
            return
        self._stats_label.setText("已经忘记这条记忆")
        self._refresh_list()

    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出记忆", "firefly_memory_export.json", "JSON (*.json)"
        )
        if not path:
            return
        try:
            records = self._all_records()
            data = {
                "format": "firefly-memory-export",
                "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "record_count": len(records),
                "records": records,
            }
            Path(path).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._stats_label.setText(f"已导出 {len(records)} 条记忆")
        except Exception as exc:
            self._stats_label.setText(f"导出失败: {exc}")

    def _on_advanced(self) -> None:
        """高级维护：一致性检查 / 修复语义索引 / 清空全部记忆。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("高级维护")
        dialog.setFixedWidth(380)
        layout = QVBoxLayout(dialog)

        info = QLabel("")
        info.setWordWrap(True)
        info.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        layout.addWidget(info)

        def _check():
            try:
                report = self._service.consistency_report()
                info.setText(
                    f"✓ 记忆系统正常\n"
                    f"Repository: {report.healthy}  |  Missing: {len(report.missing_indexes)}"
                    f"  |  Orphan: {len(report.orphan_indexes)}  |  Stale: {len(report.stale_links)}"
                )
            except Exception as exc:
                info.setText(f"一致性检查失败: {exc}")

        check_btn = QPushButton("检查记忆一致性")
        check_btn.clicked.connect(_check)
        layout.addWidget(check_btn)

        reconcile_btn = QPushButton("检查并修复语义索引")
        reconcile_btn.clicked.connect(lambda: self._do_reconcile(info))
        layout.addWidget(reconcile_btn)

        clear_btn = QPushButton("清空所有长期记忆")
        clear_btn.setStyleSheet(
            f"QPushButton {{ color: rgba(200, 80, 60, 255); background: transparent;"
            f" border: 1px solid rgba(200, 80, 60, 120); border-radius: 10px;"
            f" padding: 4px 12px; }}"
        )
        clear_btn.clicked.connect(lambda: self._do_clear_all(info))
        layout.addWidget(clear_btn)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)

        _check()
        dialog.exec()

    def _do_reconcile(self, info_label: QLabel) -> None:
        """Dry-run reconcile first; if issues found, confirm then fix."""
        try:
            report = self._service.reconcile(dry_run=True)
            if report.missing_indexes or report.orphan_indexes or report.stale_links:
                reply = QMessageBox.question(
                    self, "修复语义索引",
                    f"发现 {len(report.missing_indexes)} missing, "
                    f"{len(report.orphan_indexes)} orphan, "
                    f"{len(report.stale_links)} stale。\n执行修复？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._service.reconcile(dry_run=False)
                    info_label.setText("语义索引已修复。")
            else:
                info_label.setText("语义索引已经正常，无需修复。")
        except Exception as exc:
            info_label.setText(f"修复失败: {exc}")

    def _do_clear_all(self, info_label: QLabel) -> None:
        reply = QMessageBox.question(
            self, "清空所有长期记忆",
            "确定清空所有长期记忆？\n此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        text, ok = QInputDialog.getText(self, "二次确认", '请输入"清空"以确认：')
        if not ok or text.strip() != "清空":
            return
        try:
            self._service.clear_all()
            info_label.setText("已清空所有长期记忆。")
            self._refresh_list()
        except Exception as exc:
            info_label.setText(f"清空失败: {exc}")