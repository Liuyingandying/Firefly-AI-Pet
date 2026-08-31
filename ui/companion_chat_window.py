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

Image attachment v1: drag & drop, Ctrl+V (clipboard image), or the ``+`` file
picker add a single in-memory image. With an attachment the turn answers from
the image through DeepSeek Vision one-shot — never a screenshot, never memory,
nothing is persisted.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, Signal, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.agent_events import AgentEventType
from ui import theme
from ui.character_conversation_runner import CharacterConversationRunner
from ui.companion_attachment import (
    ALLOWED_IMAGE_EXTENSIONS,
    THUMBNAIL_SIZE,
    AttachmentError,
    AttachmentImage,
    decode_attachment_bytes,
    load_attachment_file,
    qimage_to_attachment,
    rounded_pixmap,
)

_CLIPBOARD_NAME = "剪贴板图片"


class _AttachmentLineEdit(QLineEdit):
    """QLineEdit that turns Ctrl+V-with-an-image into an attachment signal.

    Text-only pastes keep the normal QLineEdit behaviour untouched.
    """

    image_pasted = Signal(QImage)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.Paste):
            image = QApplication.clipboard().image()
            if not image.isNull():
                self.image_pasted.emit(image)
                event.accept()
                return
        super().keyPressEvent(event)


class _AttachmentChip(QFrame):
    """[ thumbnail ] filename ×  — the single in-memory image attachment."""

    remove_clicked = Signal()

    def __init__(self, attachment: AttachmentImage, parent=None):
        super().__init__(parent)
        self.attachment = attachment
        self.setStyleSheet(
            "background: rgba(244, 250, 255, 240);"
            "border: 1px solid rgba(190, 219, 237, 160);"
            "border-radius: 10px;"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        pixmap = QPixmap.fromImage(attachment.image)
        scaled = pixmap.scaled(
            THUMBNAIL_SIZE, THUMBNAIL_SIZE,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        thumb = QLabel()
        thumb.setPixmap(rounded_pixmap(scaled))
        thumb.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setAccessibleName("图片附件缩略图")

        name = QLabel(attachment.display_name)
        name.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)};"
            f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.scaled_font_px(8)}pt;"
        )
        name.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)

        close = QPushButton("×")
        close.setFixedSize(20, 20)
        close.setCursor(Qt.PointingHandCursor)
        close.setAccessibleName("移除图片附件")
        close.setToolTip("移除图片附件")
        close.clicked.connect(self.remove_clicked)

        layout.addWidget(thumb)
        layout.addWidget(name, 1)
        layout.addWidget(close)


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
        # Record the user's own window BEFORE a Firefly window takes
        # focus, so "看看我在做什么" later captures what they were doing
        # (not the Companion). Reads the HWND only; no capture, no provider.
        from core.screen_vision.foreground_tracker import foreground_tracker

        foreground_tracker.remember_current_external_window()
        window = cls._instance
        if window is None:
            window = cls(runner=runner)
            cls._instance = window
        window.show()
        foreground_tracker.remember_firefly_window(int(window.winId()))
        window.raise_()
        window.activateWindow()
        window.input.setFocus()
        return window

    def closeEvent(self, event) -> None:
        # A closed companion window may be reopened by the next Ask with a
        # fresh instance and the same conversation runner.
        if CompanionChatWindow._instance is self:
            CompanionChatWindow._instance = None
        try:
            from core.screen_vision.foreground_tracker import foreground_tracker

            foreground_tracker.forget_firefly_window(int(self.winId()))
        except RuntimeError:
            pass  # window already destroyed
        super().closeEvent(event)

    def __init__(self, runner: CharacterConversationRunner | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Firefly Companion")
        self.resize(480, 640)
        self.setAcceptDrops(True)

        self.runner = runner or CharacterConversationRunner(parent=self)
        self.runner.agent_event.connect(self._on_event)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        for message in self.runner.history:
            label = "你" if message["role"] == "user" else "流萤"
            self.log.append(f"{label}: {message['content']}")

        # Image attachment (v1): at most one in-memory image per turn.
        self._pending_attachment: AttachmentImage | None = None
        self._chip: _AttachmentChip | None = None
        self._turn_has_image = False

        self.attachment_row = QWidget(self)
        self.attachment_layout = QHBoxLayout(self.attachment_row)
        self.attachment_layout.setContentsMargins(0, 0, 0, 0)
        self.attachment_layout.setSpacing(0)
        self.attachment_row.setVisible(False)

        self.hint_label = QLabel(self)
        self.hint_label.setStyleSheet(
            f"color: {theme.css_color(theme.ERROR_STATUS)};"
            f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.scaled_font_px(8)}pt;"
        )
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(False)

        self.input = _AttachmentLineEdit()
        self.input.setPlaceholderText("和流萤说点什么…")
        self.input.image_pasted.connect(self._on_image_pasted)

        self.pick_button = QPushButton("＋")
        self.pick_button.setFixedSize(28, 28)
        self.pick_button.setCursor(Qt.PointingHandCursor)
        self.pick_button.setToolTip("选择本地图片")
        self.pick_button.setAccessibleName("选择图片附件")
        self.pick_button.clicked.connect(self._pick_image)

        self.send_button = QPushButton("发送")

        row = QHBoxLayout()
        row.addWidget(self.pick_button)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.log, 1)
        layout.addWidget(self.attachment_row)
        layout.addWidget(self.hint_label)
        layout.addLayout(row)

        self.input.returnPressed.connect(self._send)
        self.send_button.clicked.connect(self._send)

    # ------------------------------------------------------ attachment entry

    def _on_image_pasted(self, image: QImage) -> None:
        try:
            self._set_attachment(qimage_to_attachment(image, _CLIPBOARD_NAME))
        except AttachmentError as exc:
            self._show_hint(str(exc))

    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp)"
        )
        if not path:
            return
        try:
            self._set_attachment(load_attachment_file(Path(path)))
        except AttachmentError as exc:
            self._show_hint(str(exc))

    def _set_attachment_from_bytes(self, data: bytes, display_name: str) -> None:
        self._set_attachment(decode_attachment_bytes(data, display_name))

    def _set_attachment(self, attachment: AttachmentImage) -> None:
        """Replace the current attachment (v1 keeps at most one image)."""
        self._pending_attachment = attachment
        if self._chip is not None:
            self.attachment_layout.removeWidget(self._chip)
            self._chip.deleteLater()
        chip = _AttachmentChip(attachment)
        chip.remove_clicked.connect(self._clear_attachment)
        self.attachment_layout.addWidget(chip)
        self._chip = chip
        self.attachment_row.setVisible(True)
        self._hide_hint()

    def _clear_attachment(self) -> None:
        self._pending_attachment = None
        if self._chip is not None:
            self.attachment_layout.removeWidget(self._chip)
            self._chip.deleteLater()
            self._chip = None
        self.attachment_row.setVisible(False)

    def _show_hint(self, message: str) -> None:
        self.hint_label.setText(message)
        self.hint_label.setVisible(True)

    def _hide_hint(self) -> None:
        self.hint_label.setVisible(False)

    # ------------------------------------------------------ drag & drop

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _drop_has_image(event.mimeData()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        mime = event.mimeData()
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage) and not image.isNull():
                try:
                    self._set_attachment(qimage_to_attachment(image, "拖入图片"))
                    event.acceptProposedAction()
                    return
                except AttachmentError as exc:
                    self._show_hint(str(exc))
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
                continue
            try:
                self._set_attachment(load_attachment_file(path))
                event.acceptProposedAction()
                return
            except AttachmentError as exc:
                self._show_hint(str(exc))
        event.ignore()

    # ------------------------------------------------------ send / events

    def _send(self) -> None:
        if self.runner.running:
            return
        text = self.input.text().strip()
        attachment = self._pending_attachment
        if not text and attachment is None:
            return
        self.input.clear()
        if attachment is not None:
            self.log.append(f"你: [图片] {attachment.display_name}")
        if text:
            self.log.append(f"你: {text}")
        self._set_busy(True)
        self._turn_has_image = attachment is not None
        if attachment is not None:
            self.runner.ask_with_image(text, attachment)
        else:
            self.runner.ask(text)

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        self.input.setEnabled(not busy)

    def _on_event(self, event) -> None:
        if event.type is AgentEventType.FINAL:
            self.log.append(f"流萤: {event.text}")
            self._set_busy(False)
            if self._turn_has_image:
                self._clear_attachment()  # success: attachment consumed
                self._turn_has_image = False
        elif event.type is AgentEventType.ERROR:
            self.log.append(f"[错误] {event.text}")
            self._set_busy(False)
            # Failure: keep the attachment so the user can simply retry.
            self._turn_has_image = False
        elif event.type is AgentEventType.CANCELLED:
            self.log.append("[已取消]")
            self._set_busy(False)


def _drop_has_image(mime: QMimeData) -> bool:
    """Accept image mime, or local file URLs with an image extension."""
    if mime.hasImage():
        return True
    for url in mime.urls():
        if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in ALLOWED_IMAGE_EXTENSIONS:
            return True
    return False


def main() -> int:
    app = QApplication(sys.argv)
    window = CompanionChatWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
