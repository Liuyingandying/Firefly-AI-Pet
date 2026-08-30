"""Minimal Firefly companion chat window driving CompanionRuntime.

A deliberately small Qt entry point that closes the UI -> CompanionRuntime loop
without the full visual shell (no PageLens, no workflow, no global hotkey, no
``keyboard`` dependency, no Mem0 requirement). It reuses
:class:`ui.character_conversation_runner.CharacterConversationRunner`, which
delegates to ``ConversationRuntime`` -> ``CompanionRuntime``.

Run from the project root with::

    python -m ui.companion_chat_window

A real reply requires a configured provider (TJU Qwen / Zhipu GLM / DeepSeek
via ``ProviderRouter``); without keys the turn surfaces a provider error in the
log instead of crashing.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.agent_events import AgentEventType
from ui.character_conversation_runner import CharacterConversationRunner


class CompanionChatWindow(QWidget):
    """A minimal single-window chat surface for Firefly."""

    _instance: "CompanionChatWindow | None" = None

    @classmethod
    def open_singleton(cls, runner: CharacterConversationRunner | None = None) -> "CompanionChatWindow":
        """Open the existing chat window, or bring it to the front.

        This is the single entry point for the floating bubble's ``Ask…``
        action: at most one window ever exists, an already-open window is
        raised instead of duplicated, and the input gets focus so the user
        can simply start typing. No message is filled in or sent, and no
        vision/provider call happens here.
        """
        window = cls._instance
        if window is None:
            window = cls(runner=runner)
            cls._instance = window
        window.show()
        window.raise_()
        window.activateWindow()
        window.input.setFocus()
        return window

    def closeEvent(self, event) -> None:
        # A closed companion window may be reopened by the next Ask with a
        # fresh instance and the same conversation runner.
        if CompanionChatWindow._instance is self:
            CompanionChatWindow._instance = None
        super().closeEvent(event)

    def __init__(self, runner: CharacterConversationRunner | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Firefly Companion")
        self.resize(480, 640)

        self.runner = runner or CharacterConversationRunner(parent=self)
        self.runner.agent_event.connect(self._on_event)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        for message in self.runner.history:
            label = "你" if message["role"] == "user" else "流萤"
            self.log.append(f"{label}: {message['content']}")

        self.input = QLineEdit()
        self.input.setPlaceholderText("和流萤说点什么…")

        self.send_button = QPushButton("发送")

        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.log, 1)
        layout.addLayout(row)

        self.input.returnPressed.connect(self._send)
        self.send_button.clicked.connect(self._send)

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text or self.runner.running:
            return
        self.input.clear()
        self.log.append(f"你: {text}")
        self._set_busy(True)
        self.runner.ask(text)

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        self.input.setEnabled(not busy)

    def _on_event(self, event) -> None:
        if event.type is AgentEventType.FINAL:
            self.log.append(f"流萤: {event.text}")
            self._set_busy(False)
        elif event.type is AgentEventType.ERROR:
            self.log.append(f"[错误] {event.text}")
            self._set_busy(False)
        elif event.type is AgentEventType.CANCELLED:
            self.log.append("[已取消]")
            self._set_busy(False)


def main() -> int:
    app = QApplication(sys.argv)
    window = CompanionChatWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
