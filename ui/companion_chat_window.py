"""Minimal Firefly companion chat window driving CompanionRuntime.

A deliberately small Qt entry point that closes the UI -> CompanionRuntime loop
without the full visual shell (no PageLens, no workflow, no global hotkey, no
``keyboard`` dependency, no Mem0 requirement). It reuses
:class:`ui.character_conversation_runner.CharacterConversationRunner`, which
delegates to ``ConversationRuntime`` -> ``CompanionRuntime``.

Run from the project root with::

    python -m ui.companion_chat_window

Attachment v1: one in-memory attachment per turn.
- Images (drag & drop, Ctrl+V, ``+``): answered through DeepSeek Vision
  one-shot; cleared after a successful turn.
- Documents (PDF / DOCX / PPTX / TXT / MD / XLSX / CSV via drag & drop or
  ``+``): parsed locally in the background; question-relevant excerpts go to
  the text provider; the chip stays for follow-up questions until removed or
  replaced. Nothing is persisted or uploaded as a raw file.
"""

from __future__ import annotations

import sys
import threading
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
from core.document_attachment import DocumentParseError, EncryptedPdfError, parse_document_bytes
from ui import theme
from ui.character_conversation_runner import CharacterConversationRunner
from ui.companion_attachment import (
    LEGACY_DOCUMENT_MESSAGE,
    THUMBNAIL_SIZE,
    AttachmentError,
    AttachmentImage,
    DocumentAttachment,
    decode_attachment_bytes,
    detect_kind_from_path,
    load_attachment_file,
    load_document_file,
    qimage_to_attachment,
    rounded_pixmap,
)

_CLIPBOARD_NAME = "剪贴板图片"

_LARGE_FILE_BYTES = 50 * 1024 * 1024   # >=50MB shows "正在解析大型文件…"

_LEGACY_DOCUMENT_EXTENSIONS = (".doc", ".ppt", ".xls")

_DOCUMENT_KIND_LABELS = {
    "pdf": "PDF", "docx": "DOCX", "pptx": "PPTX",
    "txt": "TXT", "md": "MD", "xlsx": "XLSX", "csv": "CSV",
}


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
        close.setAccessibleName("移除附件")
        close.setToolTip("移除附件")
        close.clicked.connect(self.remove_clicked)

        layout.addWidget(thumb)
        layout.addWidget(name, 1)
        layout.addWidget(close)


class _DocumentChip(QFrame):
    """[ KIND ] name / state · ×  — the single in-memory document attachment.

    State reflects the background parse: ``Parsing…`` (``正在解析大型文件…``
    for >=50MB files) -> ``Ready`` (with page / slide / sheet count) or
    ``Unable to read``.
    """

    remove_clicked = Signal()

    def __init__(self, attachment: DocumentAttachment, *, large: bool = False, parent=None):
        super().__init__(parent)
        self.attachment = attachment
        self.large = large
        self.setStyleSheet(
            "background: rgba(244, 250, 255, 240);"
            "border: 1px solid rgba(190, 219, 237, 160);"
            "border-radius: 10px;"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(1)

        icon = QLabel(_DOCUMENT_KIND_LABELS.get(attachment.kind, "DOC"))
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(40, 28)
        icon.setStyleSheet(
            f"color: {theme.css_color(theme.CYAN_ACCENT)};"
            f"background: rgba(83, 220, 233, 26);"
            f"border-radius: 6px; font-weight: {theme.FONT_WEIGHT_MEDIUM};"
            f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.scaled_font_px(8)}pt;"
        )
        icon.setAccessibleName("文档附件图标")

        name = QLabel(attachment.display_name)
        name.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)};"
            f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.scaled_font_px(8)}pt;"
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )

        self.status_label = QLabel("正在解析大型文件…" if large else "Parsing…")
        self.status_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)};"
            f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.scaled_font_px(7)}pt;"
        )

        close = QPushButton("×")
        close.setFixedSize(20, 20)
        close.setCursor(Qt.PointingHandCursor)
        close.setAccessibleName("移除附件")
        close.setToolTip("移除附件")
        close.clicked.connect(self.remove_clicked)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(icon)
        top.addWidget(name, 1)
        top.addWidget(close)

        layout.addLayout(top)
        layout.addWidget(self.status_label)

    def set_state(self, state: str) -> None:
        context = self.attachment.context
        if state == "ready" and context is not None:
            detail = _document_count_label(context)
            self.status_label.setText(f"{detail} · Ready" if detail else "Ready")
            self.status_label.setStyleSheet(
                f"color: {theme.css_color(theme.MINT_STATUS)};"
                f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.scaled_font_px(7)}pt;"
            )
        elif state == "error":
            self.status_label.setText("Unable to read")
            self.status_label.setStyleSheet(
                f"color: {theme.css_color(theme.ERROR_STATUS)};"
                f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.scaled_font_px(7)}pt;"
            )
        else:
            self.status_label.setText("正在解析大型文件…" if self.large else "Parsing…")


def _document_count_label(context) -> str:
    if context.page_count:
        return f"{context.page_count} pages"
    if context.slide_count:
        return f"{context.slide_count} slides"
    if context.sheet_count:
        return f"{context.sheet_count} sheets"
    return ""


class CompanionChatWindow(QWidget):
    """A minimal single-window chat surface for Firefly."""

    _instance: "CompanionChatWindow | None" = None

    attachment_parsed = Signal(object, str)  # (DocumentAttachment, state)

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
        self.attachment_parsed.connect(self._on_attachment_parsed)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        for message in self.runner.history:
            label = "你" if message["role"] == "user" else "流萤"
            self.log.append(f"{label}: {message['content']}")

        # Attachment v1: at most one in-memory image OR document per turn.
        self._pending_attachment: AttachmentImage | DocumentAttachment | None = None
        self._chip: _AttachmentChip | _DocumentChip | None = None
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
        self.pick_button.setToolTip("选择图片或文档")
        self.pick_button.setAccessibleName("选择附件")
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
            self, "选择附件", "",
            "附件 (*.png *.jpg *.jpeg *.webp *.pdf *.docx *.pptx *.txt *.md *.xlsx *.csv)",
        )
        if not path:
            return
        self._add_local_file(Path(path))

    def _add_local_file(self, path: Path) -> None:
        kind = detect_kind_from_path(path)
        if kind is None:
            self._show_hint(LEGACY_DOCUMENT_MESSAGE)
            return
        try:
            if kind == "image":
                self._set_attachment(load_attachment_file(path))
            else:
                self._set_attachment(load_document_file(path))
        except AttachmentError as exc:
            self._show_hint(str(exc))

    def _set_attachment(self, attachment: AttachmentImage | DocumentAttachment) -> None:
        """Replace the current attachment (v1 keeps at most one)."""
        self._pending_attachment = attachment
        if self._chip is not None:
            self.attachment_layout.removeWidget(self._chip)
            self._chip.deleteLater()
        if isinstance(attachment, DocumentAttachment):
            chip = _DocumentChip(
                attachment, large=attachment.original_size >= _LARGE_FILE_BYTES
            )
            self._start_document_parse(attachment)
        else:
            chip = _AttachmentChip(attachment)
        chip.remove_clicked.connect(self._clear_attachment)
        self.attachment_layout.addWidget(chip)
        self._chip = chip
        self.attachment_row.setVisible(True)
        self._hide_hint()

    def _start_document_parse(self, attachment: DocumentAttachment) -> None:
        """Parse the document on a daemon thread; chip updates via signal."""

        def work() -> None:
            data = attachment.take_source_bytes()
            try:
                context = parse_document_bytes(data, attachment.kind, attachment.display_name)
                attachment.mark_ready(context)
            except EncryptedPdfError:
                attachment.mark_error("PDF 需要密码")
            except DocumentParseError as exc:
                attachment.mark_error(str(exc) or "无法读取")
            except Exception as exc:  # parser must never crash the thread
                attachment.mark_error(f"无法读取: {type(exc).__name__}")
            finally:
                attachment.release_source_bytes()
                self.attachment_parsed.emit(attachment, attachment.parse_state)

        threading.Thread(
            target=work, daemon=True, name="FireflyDocumentParse"
        ).start()

    def _on_attachment_parsed(self, attachment: DocumentAttachment, state: str) -> None:
        if self._chip is not None and getattr(self._chip, "attachment", None) is attachment:
            self._chip.set_state(state)

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
        if _drop_has_attachment(event.mimeData()):
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
            kind = detect_kind_from_path(path)
            if kind is None:
                if path.suffix.lower() in _LEGACY_DOCUMENT_EXTENSIONS:
                    # Consume the drop and explain; never silently ignore.
                    self._show_hint(LEGACY_DOCUMENT_MESSAGE)
                    event.acceptProposedAction()
                    return
                continue
            try:
                self._add_local_file(path)
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
            self.log.append(f"你: [附件] {attachment.display_name}")
        if text:
            self.log.append(f"你: {text}")
        self._set_busy(True)
        if isinstance(attachment, DocumentAttachment):
            self._turn_has_image = False
            self.runner.ask_with_document(text, attachment)
        elif attachment is not None:
            self._turn_has_image = True
            self.runner.ask_with_image(text, attachment)
        else:
            self._turn_has_image = False
            self.runner.ask(text)

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        self.input.setEnabled(not busy)

    def _on_event(self, event) -> None:
        if event.type is AgentEventType.FINAL:
            self.log.append(f"流萤: {event.text}")
            self._set_busy(False)
            if self._turn_has_image:
                self._clear_attachment()  # images: consumed on success
                self._turn_has_image = False
        elif event.type is AgentEventType.ERROR:
            self.log.append(f"[错误] {event.text}")
            self._set_busy(False)
            # Failure: keep the attachment so the user can simply retry.
            self._turn_has_image = False
        elif event.type is AgentEventType.CANCELLED:
            self.log.append("[已取消]")
            self._set_busy(False)


def _drop_has_attachment(mime: QMimeData) -> bool:
    """Accept image mime, or local file URLs with a supported extension.

    Legacy office formats (.doc/.ppt/.xls) are also accepted so the drop can
    explain why they are not supported yet.
    """
    if mime.hasImage():
        return True
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = Path(url.toLocalFile())
        if detect_kind_from_path(path) is not None:
            return True
        if path.suffix.lower() in _LEGACY_DOCUMENT_EXTENSIONS:
            return True
    return False


def main() -> int:
    app = QApplication(sys.argv)
    window = CompanionChatWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
