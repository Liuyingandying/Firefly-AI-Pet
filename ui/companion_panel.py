"""Firefly AI Companion panel — Phase 7B fast conversational UI."""

from __future__ import annotations

import html
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .process_launcher import ProcessLauncher, QuickAskRunner
from .workspace_store import WorkspaceStore


EFFORT_LABELS = {
    "low": "⚡ 快速",
    "medium": "标准",
    "high": "深度",
}


class CompanionPanel(QWidget):
    hidden = Signal()

    def __init__(self, parent_pet=None):
        super().__init__(None)
        self._pet = parent_pet
        self._store = WorkspaceStore()
        self._runner = QuickAskRunner(self)
        self._pending_user_text = ""
        self._pending_agent = ""
        self._resolved_state = {"state": "idle", "agent": None}
        self._streaming_started = False
        self._streaming_text = ""
        self._activity_base = "Ready"
        self._ask_started_at: float | None = None

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(250)
        self._elapsed_timer.timeout.connect(self._refresh_activity)

        self.setWindowTitle("Firefly AI Companion")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(460, 610)
        self.resize(500, 690)

        self._build_ui()
        self._connect_signals()
        self._reload_workspaces()
        self._load_preferences()
        self._render_status()

        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.hide)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._send_quick_ask)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._send_quick_ask)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        card = QFrame(self)
        card.setObjectName("card")
        card.setStyleSheet(
            """
            QFrame#card {
                background: rgba(22, 26, 38, 246);
                border: 1px solid rgba(138, 198, 255, 90);
                border-radius: 18px;
            }
            QLabel { color: #eef6ff; }
            QLabel#muted { color: #aab9cf; }
            QLabel#statusChip {
                background: rgba(74, 123, 190, 70);
                border: 1px solid rgba(145, 205, 255, 110);
                border-radius: 9px;
                padding: 5px 9px;
                color: #eaf7ff;
                font-weight: 600;
            }
            QPushButton {
                color: #f5fbff;
                background: rgba(70, 96, 145, 170);
                border: 1px solid rgba(154, 202, 255, 100);
                border-radius: 9px;
                padding: 8px 12px;
            }
            QPushButton:hover { background: rgba(88, 121, 182, 210); }
            QPushButton:disabled { color: #738096; background: rgba(52, 58, 72, 130); }
            QPushButton#primary { background: rgba(63, 129, 207, 210); font-weight: 600; }
            QPushButton#close { background: transparent; border: 0; font-size: 18px; padding: 3px 7px; }
            QComboBox, QTextEdit, QTextBrowser {
                color: #eaf3ff;
                background: rgba(9, 13, 22, 210);
                border: 1px solid rgba(132, 166, 214, 85);
                border-radius: 9px;
                padding: 7px;
                selection-background-color: #416fae;
            }
            QComboBox QAbstractItemView {
                background: #171d2a;
                color: #eff6ff;
                selection-background-color: #416fae;
            }
            """
        )
        root.addWidget(card)
        body = QVBoxLayout(card)
        body.setContentsMargins(18, 16, 18, 18)
        body.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("✨  Firefly AI Companion")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        close_btn = QPushButton("×")
        close_btn.setObjectName("close")
        close_btn.setToolTip("关闭面板（Esc）")
        close_btn.clicked.connect(self.hide)
        title_row.addWidget(close_btn)
        body.addLayout(title_row)

        self._status_chip = QLabel()
        self._status_chip.setObjectName("statusChip")
        self._status_chip.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        body.addWidget(self._status_chip)

        workspace_label = QLabel("工作区")
        workspace_label.setObjectName("muted")
        body.addWidget(workspace_label)
        workspace_row = QHBoxLayout()
        self._workspace_combo = QComboBox()
        self._workspace_combo.setEditable(False)
        self._workspace_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        workspace_row.addWidget(self._workspace_combo, 1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse_workspace)
        workspace_row.addWidget(browse_btn)
        body.addLayout(workspace_row)

        launch_label = QLabel("打开完整界面")
        launch_label.setObjectName("muted")
        body.addWidget(launch_label)
        launch_row = QHBoxLayout()
        codex_btn = QPushButton("⚡ Codex")
        claude_btn = QPushButton("🧠 Claude")
        chatgpt_btn = QPushButton("💬 ChatGPT")
        codex_btn.clicked.connect(lambda: self._launch_agent("codex"))
        claude_btn.clicked.connect(lambda: self._launch_agent("claude"))
        chatgpt_btn.clicked.connect(self._open_chatgpt)
        launch_row.addWidget(codex_btn)
        launch_row.addWidget(claude_btn)
        launch_row.addWidget(chatgpt_btn)
        body.addLayout(launch_row)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet("color: rgba(160,190,220,60);")
        body.addWidget(divider)

        quick_title = QLabel("快速交流")
        quick_title.setStyleSheet("font-size: 14px; font-weight: 650;")
        body.addWidget(quick_title)

        hint = QLabel("连续对话会复用 Agent 会话；默认“快速”模式降低推理强度。Quick Ask 保持只读。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        body.addWidget(hint)

        self._prompt = QTextEdit()
        self._prompt.setPlaceholderText("例如：解释一下这个项目的结构。\nCtrl+Enter 发送，Enter 换行。")
        self._prompt.setFixedHeight(90)
        body.addWidget(self._prompt)

        controls_row = QHBoxLayout()
        self._agent_combo = QComboBox()
        self._agent_combo.addItem("Codex", "codex")
        self._agent_combo.addItem("Claude", "claude")
        self._effort_combo = QComboBox()
        for effort in ("low", "medium", "high"):
            self._effort_combo.addItem(EFFORT_LABELS[effort], effort)
        controls_row.addWidget(QLabel("回答者"))
        controls_row.addWidget(self._agent_combo)
        controls_row.addSpacing(8)
        controls_row.addWidget(QLabel("模式"))
        controls_row.addWidget(self._effort_combo)
        controls_row.addStretch(1)
        body.addLayout(controls_row)

        action_row = QHBoxLayout()
        self._context_btn = QPushButton("连续对话：开启")
        self._context_btn.setCheckable(True)
        self._new_chat_btn = QPushButton("新对话")
        self._clear_btn = QPushButton("清空")
        self._stop_btn = QPushButton("停止")
        self._stop_btn.setEnabled(False)
        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("primary")
        action_row.addWidget(self._context_btn)
        action_row.addWidget(self._new_chat_btn)
        action_row.addStretch(1)
        action_row.addWidget(self._clear_btn)
        action_row.addWidget(self._stop_btn)
        action_row.addWidget(self._send_btn)
        body.addLayout(action_row)

        self._activity = QLabel("Ready")
        self._activity.setObjectName("muted")
        body.addWidget(self._activity)

        self._chat = QTextBrowser()
        self._chat.setOpenExternalLinks(True)
        self._chat.setPlaceholderText("回答会显示在这里。Claude 支持流式文字；Codex 会显示结构化进度并在完成后给出回答。")
        self._chat.setMinimumHeight(220)
        body.addWidget(self._chat, 1)

        note = QLabel("会话 ID 只保存在当前桌宠进程内；不保存 prompt、回答或 API Key。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        body.addWidget(note)

    def _connect_signals(self) -> None:
        self._workspace_combo.currentIndexChanged.connect(self._workspace_changed)
        self._send_btn.clicked.connect(self._send_quick_ask)
        self._stop_btn.clicked.connect(self._runner.stop)
        self._clear_btn.clicked.connect(self._clear_chat)
        self._new_chat_btn.clicked.connect(self._new_conversation)
        self._context_btn.toggled.connect(self._context_toggled)
        self._effort_combo.currentIndexChanged.connect(self._effort_changed)
        self._runner.started.connect(self._on_ask_started)
        self._runner.partial.connect(self._on_partial)
        self._runner.status.connect(self._set_activity_base)
        self._runner.failed.connect(self._on_ask_failed)
        self._runner.finished.connect(self._on_ask_finished)
        self._runner.session_changed.connect(self._on_session_changed)

    def _load_preferences(self) -> None:
        effort_index = self._effort_combo.findData(self._store.quick_ask_effort)
        self._effort_combo.setCurrentIndex(max(0, effort_index))
        self._context_btn.setChecked(self._store.conversation_enabled)
        self._context_btn.setText("连续对话：开启" if self._context_btn.isChecked() else "连续对话：关闭")

    def update_state(self, resolved: dict) -> None:
        self._resolved_state = dict(resolved)
        self._render_status()

    def _render_status(self) -> None:
        state = str(self._resolved_state.get("state") or "idle").title()
        agent = self._resolved_state.get("agent")
        if agent:
            text = f"{str(agent).title()} · {state}"
        elif state.lower() == "sleeping":
            text = "All agents sleeping"
        else:
            text = state
        self._status_chip.setText(f"当前状态：{text}")

    def _reload_workspaces(self) -> None:
        self._workspace_combo.blockSignals(True)
        self._workspace_combo.clear()
        current = self._store.current_workspace
        items = self._store.recent_workspaces
        if current not in items:
            items.insert(0, current)
        for path in items:
            self._workspace_combo.addItem(str(path), str(path))
        index = self._workspace_combo.findData(str(current))
        self._workspace_combo.setCurrentIndex(max(0, index))
        self._workspace_combo.blockSignals(False)

    def current_workspace(self) -> Path:
        data = self._workspace_combo.currentData()
        return Path(data) if data else self._store.current_workspace

    def _workspace_changed(self, index: int) -> None:
        if index < 0:
            return
        data = self._workspace_combo.itemData(index)
        if not data:
            return
        try:
            self._store.set_workspace(data)
            self._set_activity_base("工作区已切换；连续会话按工作区隔离。")
        except ValueError as exc:
            self._show_error(str(exc))
            self._reload_workspaces()

    def _browse_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 AI 工作区", str(self.current_workspace()))
        if not selected:
            return
        try:
            self._store.set_workspace(selected)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._reload_workspaces()

    def _launch_agent(self, agent: str) -> None:
        ok, message = ProcessLauncher.launch_agent(agent, self.current_workspace())
        self._set_activity_base(message)
        if not ok:
            self._show_error(message)

    def _open_chatgpt(self) -> None:
        ok, message = ProcessLauncher.open_chatgpt()
        self._set_activity_base(message)
        if not ok:
            self._show_error(message)

    def _effort_changed(self, _index: int) -> None:
        effort = str(self._effort_combo.currentData() or "low")
        try:
            self._store.set_quick_ask_effort(effort)
        except ValueError:
            pass

    def _context_toggled(self, checked: bool) -> None:
        self._context_btn.setText("连续对话：开启" if checked else "连续对话：关闭")
        self._store.set_conversation_enabled(checked)
        if not checked:
            self._set_activity_base("连续对话已关闭；下一问将使用独立临时会话。")
        else:
            self._set_activity_base("连续对话已开启；后续会复用当前 Agent 会话。")

    def _new_conversation(self) -> None:
        if self._runner.running:
            self._set_activity_base("请先停止当前 Quick Ask。")
            return
        agent = str(self._agent_combo.currentData())
        self._runner.clear_session(agent, self.current_workspace())
        self._append_message("系统", f"已为 {agent.title()} 开始新的连续对话。")
        self._set_activity_base("新对话已就绪。")

    def _send_quick_ask(self) -> None:
        if self._runner.running:
            return
        user_text = self._prompt.toPlainText().strip()
        if not user_text:
            self._set_activity_base("请输入问题。")
            return
        agent = str(self._agent_combo.currentData())
        effort = str(self._effort_combo.currentData() or "low")
        self._pending_user_text = user_text
        self._pending_agent = agent
        self._streaming_started = False
        self._streaming_text = ""
        self._append_message("你", user_text)
        self._prompt.clear()
        if not self._runner.ask(
            agent,
            user_text,
            self.current_workspace(),
            effort=effort,
            persistent=self._context_btn.isChecked(),
        ):
            self._pending_user_text = ""
            self._pending_agent = ""

    def _on_ask_started(self, agent: str) -> None:
        self._send_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._agent_combo.setEnabled(False)
        self._effort_combo.setEnabled(False)
        self._workspace_combo.setEnabled(False)
        self._context_btn.setEnabled(False)
        self._new_chat_btn.setEnabled(False)
        self._ask_started_at = time.monotonic()
        self._elapsed_timer.start()
        self._set_activity_base(f"正在连接 {agent.title()}…")

    def _on_partial(self, text: str) -> None:
        if not text:
            return
        if not self._streaming_started:
            sender = (self._pending_agent or "Agent").title()
            self._chat.append(f'<b style="color:#a9d7ff;">{html.escape(sender)}</b>')
            self._streaming_started = True
        self._streaming_text += text
        cursor = self._chat.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self._chat.setTextCursor(cursor)
        self._chat.ensureCursorVisible()

    def _on_ask_failed(self, message: str) -> None:
        self._set_activity_base("Error")
        self._append_message("系统", message)

    def _on_ask_finished(self, stdout: str, exit_code: int) -> None:
        self._elapsed_timer.stop()
        self._send_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._agent_combo.setEnabled(True)
        self._effort_combo.setEnabled(True)
        self._workspace_combo.setEnabled(True)
        self._context_btn.setEnabled(True)
        self._new_chat_btn.setEnabled(True)

        if exit_code == 0 and stdout:
            if not self._streaming_started:
                self._append_message((self._pending_agent or "Agent").title(), stdout.strip())
            else:
                cursor = self._chat.textCursor()
                cursor.movePosition(QTextCursor.End)
                cursor.insertText("\n")
                self._chat.setTextCursor(cursor)
        self._ask_started_at = None
        self._pending_user_text = ""
        self._pending_agent = ""
        self._streaming_started = False
        self._streaming_text = ""
        self._refresh_activity()

    def _on_session_changed(self, agent: str, _session_id: str) -> None:
        if self._context_btn.isChecked():
            self._set_activity_base(f"{agent.title()} 连续会话已建立；下一问将直接续接。")

    def _set_activity_base(self, text: str) -> None:
        self._activity_base = text
        self._refresh_activity()

    def _refresh_activity(self) -> None:
        if self._ask_started_at is not None and self._runner.running:
            elapsed = time.monotonic() - self._ask_started_at
            self._activity.setText(f"{self._activity_base}  ·  {elapsed:.1f}s")
        else:
            self._activity.setText(self._activity_base)

    def _append_message(self, sender: str, text: str) -> None:
        safe_sender = html.escape(sender)
        safe_text = html.escape(text).replace("\n", "<br>")
        self._chat.append(
            f'<div style="margin:6px 0 12px 0;">'
            f'<b style="color:#a9d7ff;">{safe_sender}</b><br>'
            f'<span style="color:#edf5ff;">{safe_text}</span></div>'
        )
        bar = self._chat.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _clear_chat(self) -> None:
        if self._runner.running:
            self._set_activity_base("运行中不能清空。")
            return
        self._chat.clear()
        self._set_activity_base("Ready")

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "Firefly AI Companion", message)

    def toggle_near_pet(self) -> None:
        if self.isVisible():
            self.hide()
            return
        self.reposition_near_pet()
        self.show()
        self.raise_()
        self.activateWindow()

    def reposition_near_pet(self) -> None:
        if self._pet is None:
            return
        screen = QApplication.screenAt(self._pet.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self._pet.frameGeometry()
        x = pet_geo.left() - self.width() - 14
        y = pet_geo.bottom() - self.height()
        if x < available.left():
            x = pet_geo.right() + 14
        if x + self.width() > available.right():
            x = available.right() - self.width()
        if y < available.top():
            y = available.top()
        if y + self.height() > available.bottom():
            y = available.bottom() - self.height()
        self.move(x, y)

    def hideEvent(self, event) -> None:
        self.hidden.emit()
        super().hideEvent(event)

    def shutdown(self) -> None:
        self._elapsed_timer.stop()
        self._runner.shutdown()
