"""Minimal Learning Bridge entry dialog (Phase 11).

不做课程管理页面：列出托管课程（标题 + 编排状态）、导入 PDF、
开始/继续学习（调 learning.launcher.launch_learning_mode）。
所有学习进度展示走 teach-mcp（在 Z Code 会话中），本对话框不显示也不缓存
任何 mastery/progress 值（MEMORY_OWNERSHIP contract）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.settings_manager import SettingsManager
from learning.bridge_state import (
    PROCESSING,
    action_for_state,
    derive_state,
    state_summary,
)
from learning.diagnostics import log_marker
from learning.identity import ensure_learner_id
from learning.launcher import LaunchError, launch_learning_mode
from learning.resource_manager import (
    ResourceManager,
    ResourceImportError,
    default_courses_root,
)

_BUTTON_LABELS = {
    "new": "开始学习",
    "review": "审核知识包",
    "resume": "继续学习",
}


class LearningBridgeDialog(QDialog):
    """学习模式（Z Code 桥）最小入口。"""

    course_launched = Signal(object)  # LaunchResult — shell starts the watcher
    course_presentation_requested = Signal(str)  # course_id — Firefly ChatView PoC

    def __init__(
        self,
        *,
        settings: SettingsManager | None = None,
        courses_root: Path | None = None,
        presentation_enabled: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._settings = settings or SettingsManager()
        self._presentation_enabled = presentation_enabled
        self._manager = (
            ResourceManager(courses_root)
            if courses_root is not None
            else ResourceManager()
        )
        self.setWindowTitle("学习模式" if presentation_enabled else "学习模式（Z Code 桥）")
        self.resize(460, 360)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        hint = QLabel(
            "课程资料托管在 Firefly 数据目录；学习状态唯一来源是 teach-mcp。\n"
            + ("自动控制原理可按全书草稿章节预览；草稿活动进度独立于正式掌握度。其他课程继续现有教学轮次。"
               if self._presentation_enabled else
               "开始/继续学习会启动 Z Code 并进入 firefly-learning skill。")
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._course_list = QListWidget(self)
        self._course_list.currentItemChanged.connect(
            lambda _cur, _prev: self._sync_launch_button()
        )
        layout.addWidget(self._course_list, 1)

        buttons = QHBoxLayout()
        self._import_button = QPushButton("导入学习资料", self)
        self._import_button.clicked.connect(self._on_import)
        buttons.addWidget(self._import_button)
        buttons.addStretch(1)
        self._launch_button = QPushButton("开始学习", self)
        self._launch_button.setEnabled(False)
        self._launch_button.clicked.connect(self._on_launch)
        buttons.addWidget(self._launch_button)
        self._refresh_button = QPushButton("刷新", self)
        self._refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self._refresh_button)
        layout.addLayout(buttons)

    # ------------------------------------------------------------ data

    def refresh(self) -> None:
        self._course_list.clear()
        for summary in self._manager.list_courses():
            state = derive_state(summary.course_dir)
            item = QListWidgetItem(f"{summary.title}   ·   {state_summary(summary.course_dir, state)}")
            item.setData(Qt.ItemDataRole.UserRole + 1, summary.course_id)
            item.setData(Qt.ItemDataRole.UserRole + 2, state)
            self._course_list.addItem(item)
        self._sync_launch_button()

    def _selected_course(self) -> tuple[str, str] | None:
        item = self._course_list.currentItem()
        if item is None:
            return None
        return (
            item.data(Qt.ItemDataRole.UserRole + 1),
            item.data(Qt.ItemDataRole.UserRole + 2),
        )

    def _sync_launch_button(self) -> None:
        selection = self._selected_course()
        if selection is None:
            self._launch_button.setEnabled(False)
            self._launch_button.setText("开始学习")
            return
        _course_id, state = selection
        action = action_for_state(state)
        if action is None:
            self._launch_button.setEnabled(False)
            self._launch_button.setText("处理中…")
            return
        self._launch_button.setEnabled(True)
        self._launch_button.setText(_BUTTON_LABELS.get(action, "开始学习"))

    # ------------------------------------------------------------ actions

    def _request_presentation(self, course_id: str) -> None:
        self.course_presentation_requested.emit(course_id)
        self.accept()

    def _on_import(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择学习资料（PDF）", "", "PDF (*.pdf)"
        )
        if not path:
            return
        try:
            learner_id = ensure_learner_id(self._settings)
            result = self._manager.import_pdf(Path(path), learner_id)
        except ResourceImportError as exc:
            QMessageBox.warning(self, "导入失败", f"[{exc.code}] {exc.message}")
            return
        except Exception as exc:  # noqa: BLE001 — UI boundary, report honestly
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self.refresh()
        for row in range(self._course_list.count()):
            item = self._course_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole + 1) == result.course_id:
                self._course_list.setCurrentRow(row)
                break
        if result.deduplicated:
            QMessageBox.information(
                self,
                "已存在托管课程",
                f"相同内容（SHA256 一致）已导入为课程 {result.course_id}，未产生重复副本。",
            )

    def _on_launch(self) -> None:
        selection = self._selected_course()
        if selection is None:
            return
        course_id, state = selection
        action = action_for_state(state)
        if action is None:
            return
        if action == "resume" and self._presentation_enabled:
            self._request_presentation(course_id)
            return
        try:
            result = launch_learning_mode(
                course_id,
                action,
                settings=self._settings,
                courses_root=self._manager.courses_root,
            )
        except LaunchError as exc:
            QMessageBox.warning(
                self, "Z Code 学习代理启动失败", f"[{exc.code}] {exc.message}"
            )
            return
        except Exception as exc:  # noqa: BLE001 — UI boundary, report honestly
            QMessageBox.warning(self, "Z Code 学习代理启动失败", str(exc))
            return
        title = ""
        for summary in self._manager.list_courses():
            if summary.course_id == course_id:
                title = summary.title
                break
        # Runtime Delivery fix (Phase 1/2): emit FIRST (watcher must attach
        # before anything modal can run) and NEVER block the return channel
        # behind a modal success box — the chat window shows progress instead.
        if result.spawned:
            self.course_launched.emit(result)
            log_marker("COURSE_LAUNCHED_EMIT", course_id=course_id, task_id=result.plan.task_id)
            self.accept()  # hand the user straight back to the chat window
        else:
            QMessageBox.warning(
                self, "Z Code 学习代理启动失败", "任务未能创建（dry-run 或启动器拒绝）"
            )
