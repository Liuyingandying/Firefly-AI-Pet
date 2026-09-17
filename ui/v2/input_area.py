"""InputArea — "AI Terminal" composer for the companion console (UI V2).

Phase UI-4B-2: warm paper card matching the ChatView redesign — white card,
15px radius, hairline border, very light neutral shadow, generous padding.
Quick actions are small low-saturation text buttons (no large emoji, no
neon); 深度思考 is a low-key toggle; 发送 uses the soft blue accent.

Placeholder buttons emit signals only — no real features are wired in this
phase. Send behavior is unchanged: Enter (without Shift) or the 发送 button
emits ``send_requested`` with the current text.

The console keeps calling ``text() / setText() / clear() /
setPlaceholderText() / setFocus()`` on this widget, so it is a drop-in
replacement for the previous QLineEdit.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import theme


class InputArea(QWidget):
    """Warm paper composer card. Send logic stays in the console."""

    send_requested = Signal(str)
    file_requested = Signal()
    screenshot_requested = Signal()
    voice_requested = Signal()
    quick_command_requested = Signal()
    deep_think_toggled = Signal(bool)

    _QUICK_ACTIONS = (
        ("file", "文件", "file_requested"),
        ("screenshot", "截图", "screenshot_requested"),
        ("voice", "语音", "voice_requested"),
        ("quick", "快捷指令", "quick_command_requested"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inputArea")
        self.setStyleSheet(
            f"#inputArea {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f"  border-radius: 15px;"
            f"}}"
        )

        # Very light neutral shadow (no glow, no colored halo).
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(12)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(90, 80, 60, 28))
        self.setGraphicsEffect(shadow)

        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setPlaceholderText("输入你的问题…")
        self.text_edit.setFixedHeight(56)
        self.text_edit.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  background: rgba{theme.V2.BACKGROUND};"
            f"  border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f"  border-radius: 10px; padding: 6px 10px;"
            f"  color: rgba{theme.V2.TEXT_MAIN};"
            f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
            f"}}"
            f"QPlainTextEdit:focus {{"
            f"  border: 1px solid rgba{theme.V2.PRIMARY_BLUE};"
            f"}}"
        )

        # Small low-saturation text buttons (left side): no large emoji.
        def _tool_button(label: str) -> QToolButton:
            button = QToolButton(self)
            button.setText(label)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QToolButton {{"
                f"  color: rgba{theme.V2.TEXT_SECONDARY};"
                f"  background: transparent; border: none; padding: 2px 6px;"
                f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
                f"}}"
                f"QToolButton:hover {{"
                f"  color: rgba{theme.V2.TEXT_MAIN};"
                f"  border-radius: 6px; background: rgba{theme.V2.GLOW_BLUE};"
                f"}}"
            )
            return button

        tools_row = QHBoxLayout()
        tools_row.setSpacing(4)
        for action_id, label, signal_name in self._QUICK_ACTIONS:
            button = _tool_button(label)
            button.clicked.connect(getattr(self, signal_name).emit)
            setattr(self, f"_{action_id}_button", button)
            tools_row.addWidget(button)
        tools_row.addStretch(1)

        # 深度思考: low-key toggle with a soft violet accent when checked.
        self._deep_think = QToolButton(self)
        self._deep_think.setText("深度思考")
        self._deep_think.setCheckable(True)
        self._deep_think.setCursor(Qt.PointingHandCursor)
        self._deep_think.setStyleSheet(
            f"QToolButton {{"
            f"  color: rgba{theme.V2.TEXT_SECONDARY};"
            f"  background: transparent;"
            f"  border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f"  border-radius: 9px; padding: 3px 10px;"
            f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
            f"}}"
            f"QToolButton:checked {{"
            f"  color: rgba{theme.V2.ACCENT_PURPLE};"
            f"  background: rgba{theme.V2.GLOW_PURPLE};"
            f"  border: 1px solid rgba{theme.V2.ACCENT_PURPLE};"
            f"}}"
        )
        self._deep_think.toggled.connect(self.deep_think_toggled.emit)
        tools_row.addWidget(self._deep_think)

        # 发送: small soft-blue primary button (right side).
        self.send_button = QPushButton("发送", self)
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.setStyleSheet(
            f"QPushButton {{"
            f"  background: rgba{theme.V2.PRIMARY_BLUE};"
            f"  color: rgba(255, 255, 255, 255);"
            f"  border: none; border-radius: 9px; padding: 5px 18px;"
            f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt; font-weight: 700;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba{theme.V2.ACCENT_PURPLE};"
            f"}}"
        )
        self.send_button.clicked.connect(self._emit_send)
        tools_row.addWidget(self.send_button)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)  # 大留白
        root.setSpacing(6)
        root.addWidget(self.text_edit)
        root.addLayout(tools_row)

        # Enter 发送发生在 text_edit 上（真实键盘焦点所在），用事件过滤器
        # 拦截；Shift+Enter 不拦截，走 QPlainTextEdit 默认换行。
        self.text_edit.installEventFilter(self)
        # IME composition state: while Chinese composition is active the
        # QPlainTextEdit document is still empty, so Qt keeps drawing the
        # placeholder underneath the preedit at the top-left — two text
        # layers ("重影"). We suppress the placeholder for the duration of
        # the composition and restore the console's value when it ends.
        self._placeholder_saved: str | None = None

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        if watched is self.text_edit and event.type() == QEvent.InputMethod:
            # Never touch the composition itself: only flip the placeholder.
            preedit = event.preeditString()
            if preedit and self._placeholder_saved is None:
                current = self.text_edit.placeholderText()
                if current:
                    self._placeholder_saved = current
                    self.text_edit.setPlaceholderText("")
            elif not preedit and self._placeholder_saved is not None:
                self.text_edit.setPlaceholderText(self._placeholder_saved)
                self._placeholder_saved = None
        if watched is self.text_edit and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
                event.modifiers() & Qt.ShiftModifier
            ):
                # 先 emit 后清空：console._send 是同步连接，会从输入框读取文本。
                text = self.text().strip()
                if text:
                    self.send_requested.emit(text)
                self.clear()
                return True  # 已消费，不再插入换行
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------ behavior

    def _emit_send(self) -> None:
        text = self.text().strip()
        if not text:
            return
        self.send_requested.emit(text)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        """Enter（无 Shift）发送并清空输入框；Shift+Enter 插入换行。"""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
            event.modifiers() & Qt.ShiftModifier
        ):
            # 先 emit 后清空（console._send 同步读输入框文本）。
            text = self.text().strip()
            if text:
                self.send_requested.emit(text)
            self.clear()
            event.accept()
            return
        super().keyPressEvent(event)

    # --------------------------------------- QLineEdit-compatible surface
    # The console previously used a QLineEdit; these delegates keep every
    # existing call site (and tests) working unchanged.

    def text(self) -> str:
        return self.text_edit.toPlainText()

    def setText(self, text: str) -> None:
        self.text_edit.setPlainText(text or "")

    def clear(self) -> None:
        self.text_edit.clear()

    def setPlaceholderText(self, text: str) -> None:
        # The console is installing a new prompt — drop any stale
        # composition-suppression state so the new text is honored.
        self._placeholder_saved = None
        self.text_edit.setPlaceholderText(text)

    def setFocus(self) -> None:  # type: ignore[override]
        self.text_edit.setFocus()


__all__ = ["InputArea"]
