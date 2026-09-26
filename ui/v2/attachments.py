"""ComposerAttachments — v2 控制台 composer 的多模态附件条（ui美化风格）.

一个 composer 同时最多携带一个内存附件：一张图片（png/jpg/jpeg/webp）
或一份文档（pdf/docx/pptx/txt/md/xlsx/csv，旧版 .doc/.ppt/.xls 给出解释）。
入口共三个：「文件」胶囊（InputArea 工具栏）、Ctrl+V 剪贴板图片、拖放
（输入卡或控制台整窗）。

文档在守护线程本地解析（PDF 走懒 OCR 快索引），chip 实时显示解析状态；
解析与隐私规则完全复用 ``ui.companion_attachment`` / ``core.document_attachment``
（永不写盘、历史只存占位符），这里只做 v2 暖纸皮肤的呈现与线程编排。
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ui import theme
from ui.companion_attachment import (
    THUMBNAIL_SIZE,
    AttachmentError,
    AttachmentImage,
    DocumentAttachment,
    LEGACY_DOCUMENT_MESSAGE,
    detect_kind_from_path,
    load_attachment_file,
    load_document_file,
    qimage_to_attachment,
    rounded_pixmap,
)

_CLIPBOARD_NAME = "剪贴板图片"
_LARGE_FILE_BYTES = 50 * 1024 * 1024  # ≥50MB 时 chip 先显示「正在解析大型文件…」
_LEGACY_DOCUMENT_EXTENSIONS = (".doc", ".ppt", ".xls")

_DOCUMENT_KIND_LABELS = {
    "pdf": "PDF", "docx": "DOCX", "pptx": "PPTX",
    "txt": "TXT", "md": "MD", "xlsx": "XLSX", "csv": "CSV",
}

_PICK_FILTER = (
    "附件 (*.png *.jpg *.jpeg *.webp *.pdf *.docx *.pptx *.txt *.md *.xlsx *.csv);;"
    "图片 (*.png *.jpg *.jpeg *.webp);;文档 (*.pdf *.docx *.pptx *.txt *.md *.xlsx *.csv)"
)

_ERROR_COLOR = "#D64541"

_CHIP_QSS = (
    f"QFrame {{"
    f"  background: rgba(155, 109, 255, 18);"
    f"  border: 1px solid rgba(155, 109, 255, 70);"
    f"  border-radius: 10px;"
    f"}}"
)

_CLOSE_QSS = (
    "QPushButton {"
    f"  color: rgba{theme.V2.TEXT_SECONDARY};"
    "  background: transparent; border: none; border-radius: 10px;"
    f"  font-size: {theme.V2.FONT_BODY + 2}pt;"
    "}"
    "QPushButton:hover {"
    "  background: rgba(155, 109, 255, 40);"
    f"  color: rgba{theme.V2.PRIMARY};"
    "}"
)


def _label(text: str, *, color: str, size: int, weight: str = "normal") -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {color}; background: transparent; border: none;"
        f"font-family: {theme.V2_FONT_STACK}; font-size: {size}pt; font-weight: {weight};"
    )
    return label


class _ImageChip(QFrame):
    """[ 缩略图 ] 文件名 × — 内存图片附件。"""

    remove_clicked = Signal()

    def __init__(self, attachment: AttachmentImage, parent=None) -> None:
        super().__init__(parent)
        self.attachment = attachment
        self.setStyleSheet(_CHIP_QSS)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        scaled = QPixmap.fromImage(attachment.image).scaled(
            THUMBNAIL_SIZE, THUMBNAIL_SIZE,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        thumb = QLabel()
        thumb.setPixmap(rounded_pixmap(scaled))
        thumb.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        thumb.setAlignment(Qt.AlignCenter)

        name = _label(
            attachment.display_name,
            color=f"rgba{theme.V2.TEXT_MAIN}", size=theme.V2.FONT_CAPTION,
        )
        name.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)

        close = self._close_button()
        close.clicked.connect(self.remove_clicked)

        layout.addWidget(thumb)
        layout.addWidget(name, 1)
        layout.addWidget(close)

    def _close_button(self) -> QPushButton:
        close = QPushButton("×")
        close.setFixedSize(20, 20)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("移除附件")
        close.setStyleSheet(_CLOSE_QSS)
        return close


class _DocumentChip(QFrame):
    """[ 类型徽标 ] 文件名 / 解析状态 · × — 文档附件（状态随后台解析更新）。"""

    remove_clicked = Signal()

    def __init__(self, attachment: DocumentAttachment, *, large: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.attachment = attachment
        self.large = large
        self.setStyleSheet(_CHIP_QSS)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 4, 6, 4)
        outer.setSpacing(8)

        badge = _label(
            _DOCUMENT_KIND_LABELS.get(attachment.kind, "DOC"),
            color=f"rgba{theme.V2.PRIMARY}", size=theme.V2.FONT_CAPTION - 1,
            weight="600",
        )
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(40, 28)
        badge.setStyleSheet(
            f"color: rgba{theme.V2.PRIMARY}; background: rgba(155, 109, 255, 30);"
            "border: none; border-radius: 6px;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION - 1}pt;"
            "font-weight: 600;"
        )

        close = QPushButton("×")
        close.setFixedSize(20, 20)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("移除附件")
        close.setStyleSheet(_CLOSE_QSS)
        close.clicked.connect(self.remove_clicked)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        column.addWidget(_label(
            attachment.display_name,
            color=f"rgba{theme.V2.TEXT_MAIN}", size=theme.V2.FONT_CAPTION, weight="600",
        ))
        self.status_label = _label(
            "正在解析大型文件…" if large else "解析中…",
            color=f"rgba{theme.V2.TEXT_SECONDARY}", size=theme.V2.FONT_CAPTION - 2,
        )
        column.addWidget(self.status_label)

        outer.addWidget(badge)
        outer.addLayout(column, 1)
        outer.addWidget(close)

    def set_state(self, state: str) -> None:
        context = self.attachment.context
        lazy = getattr(self.attachment, "lazy_state", None)
        ok_color = f"rgba{theme.V2.TEXT_SECONDARY}"
        if state == "ready" and lazy is not None:
            self.status_label.setText(f"{lazy.page_count} 页 · 就绪 · OCR 按需")
            self.status_label.setStyleSheet(self._status_qss(ok_color))
        elif state == "ready" and context is not None:
            detail = _document_count_label(context)
            self.status_label.setText(f"{detail} · 就绪" if detail else "就绪")
            self.status_label.setStyleSheet(self._status_qss(ok_color))
        elif state == "error":
            self.status_label.setText("无法读取")
            self.status_label.setStyleSheet(self._status_qss(_ERROR_COLOR))
        else:
            self.status_label.setText("正在解析大型文件…" if self.large else "解析中…")
            self.status_label.setStyleSheet(self._status_qss(ok_color))

    def set_ocr_page(self, page: int) -> None:
        """按需 OCR 单页进行中的瞬时提示。"""
        self.status_label.setText(f"OCR 第 {page} 页…")

    def set_ocr_progress(self, completed: int, total: int) -> None:
        """后台整本 OCR 进度：OCR 34 / 168 → 168 页 · 就绪。"""
        if total <= 0:
            return
        if completed >= total:
            self.status_label.setText(f"{total} 页 · 就绪")
        else:
            self.status_label.setText(f"OCR {completed} / {total}")

    @staticmethod
    def _status_qss(color: str) -> str:
        return (
            f"color: {color}; background: transparent; border: none;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION - 2}pt;"
        )


def _document_count_label(context) -> str:
    if getattr(context, "page_count", 0):
        return f"{context.page_count} 页"
    if getattr(context, "slide_count", 0):
        return f"{context.slide_count} 张幻灯片"
    if getattr(context, "sheet_count", 0):
        return f"{context.sheet_count} 张工作表"
    if getattr(context, "word_count", 0):
        return f"约 {context.word_count} 字"
    return ""


def parse_document_async(attachment: DocumentAttachment, on_state) -> None:
    """守护线程解析文档；每次状态变化回调 ``on_state(attachment, state)``。

    PDF 走懒 OCR 快索引：检测到扫描页时立即「就绪（OCR 按需）」并保留
    源字节；纯原生 PDF 与其他格式走 eager 解析。解析永不崩溃线程。
    """
    from core.document_attachment import DocumentParseError, EncryptedPdfError, parse_document_bytes

    def work() -> None:
        data = attachment.take_source_bytes()
        try:
            if attachment.kind == "pdf" and data:
                from core.lazy_pdf_ocr import LazyPdfOcrState
                from core.pdf_processor import PdfPageStatus, build_pdf_lazy_index

                index = build_pdf_lazy_index(data, attachment.display_name)
                has_pending = any(
                    state != PdfPageStatus.NATIVE for state in index.page_states.values()
                )
                if has_pending:
                    import uuid

                    lazy = LazyPdfOcrState(index, generation_id=uuid.uuid4().hex)
                    attachment.attach_lazy_state(lazy)
                    attachment.mark_lazy_ready(lazy.build_context())
                else:
                    context = parse_document_bytes(data, "pdf", attachment.display_name)
                    attachment.retain_source_bytes()  # Document Vision 渲染页面用
                    attachment.mark_ready(context)
            else:
                context = parse_document_bytes(data, attachment.kind, attachment.display_name)
                if attachment.kind == "pptx":
                    attachment.retain_source_bytes()  # Document Vision 渲染幻灯片用
                attachment.mark_ready(context)
        except EncryptedPdfError:
            attachment.mark_error("PDF 需要密码")
        except DocumentParseError as exc:
            attachment.mark_error(str(exc) or "无法读取")
        except Exception as exc:  # noqa: BLE001 - parser must never crash the thread
            attachment.mark_error(f"无法读取: {type(exc).__name__}")
        finally:
            if not attachment.is_lazy and not attachment.retains_source:
                attachment.release_source_bytes()
            on_state(attachment, attachment.parse_state)

    threading.Thread(target=work, daemon=True, name="FireflyV2DocParse").start()


def mime_has_attachment(mime) -> bool:
    """拖放预检：图片 mime 或受支持的本地文件 URL（旧版格式也算，好给出解释）。"""
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


class ComposerAttachments(QWidget):
    """输入卡内的附件条：0 或 1 个 chip + 错误提示行（空时整体隐藏）。"""

    attachment_parsed = Signal(object, str)  # (DocumentAttachment, parse_state)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pending: AttachmentImage | DocumentAttachment | None = None
        self._chip: _ImageChip | _DocumentChip | None = None

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._hint = _label(
            "", color=_ERROR_COLOR, size=theme.V2.FONT_CAPTION - 1,
        )
        self._hint.setWordWrap(True)
        self._hint.setVisible(False)
        self._layout.addWidget(self._hint)
        self.setVisible(False)

    # ------------------------------------------------------------ 状态

    @property
    def pending(self) -> AttachmentImage | DocumentAttachment | None:
        return self._pending

    def consume(self) -> None:
        """清除当前附件（发送完成后由宿主调用；先停掉在途 OCR）。"""
        if self._pending is not None:
            cancel = getattr(self._pending, "cancel_lazy", None)
            if cancel is not None:
                cancel()
        self._pending = None
        if self._chip is not None:
            self._layout.removeWidget(self._chip)
            self._chip.deleteLater()
            self._chip = None
        self.setVisible(False)

    # ------------------------------------------------------------ 入口

    def pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择附件", "", _PICK_FILTER)
        if not path:
            return
        self.add_local_file(Path(path))

    def add_local_file(self, path: Path) -> None:
        kind = detect_kind_from_path(path)
        if kind is None:
            if path.suffix.lower() in _LEGACY_DOCUMENT_EXTENSIONS:
                self.show_hint(LEGACY_DOCUMENT_MESSAGE)
            else:
                self.show_hint("暂不支持这种文件类型。")
            return
        try:
            if kind == "image":
                self.set_attachment(load_attachment_file(path))
            else:
                self.set_attachment(load_document_file(path))
        except AttachmentError as exc:
            self.show_hint(str(exc))

    def add_image(self, image: QImage, name: str) -> None:
        try:
            self.set_attachment(qimage_to_attachment(image, name))
        except AttachmentError as exc:
            self.show_hint(str(exc))

    def handle_mime(self, mime) -> bool:
        """处理一次拖放 mime；可处理返回 True（宿主 accept），否则 False。"""
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage) and not image.isNull():
                self.add_image(image, "拖入图片")
                return True
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if detect_kind_from_path(path) is None:
                if path.suffix.lower() in _LEGACY_DOCUMENT_EXTENSIONS:
                    self.show_hint(LEGACY_DOCUMENT_MESSAGE)
                    return True  # 消费这次拖放，把解释展示出来
                continue
            self.add_local_file(path)
            return True
        return False

    # ------------------------------------------------------------ 呈现

    def set_attachment(self, attachment: AttachmentImage | DocumentAttachment) -> None:
        """替换当前附件（一个 composer 同时最多一个）。"""
        if self._pending is not None:
            cancel = getattr(self._pending, "cancel_lazy", None)
            if cancel is not None:
                cancel()
        self._pending = attachment
        if self._chip is not None:
            self._layout.removeWidget(self._chip)
            self._chip.deleteLater()
            self._chip = None
        if isinstance(attachment, DocumentAttachment):
            chip: _ImageChip | _DocumentChip = _DocumentChip(
                attachment, large=attachment.original_size >= _LARGE_FILE_BYTES,
            )
            parse_document_async(attachment, self._on_parse_state)
        else:
            chip = _ImageChip(attachment)
        chip.remove_clicked.connect(self.consume)
        self._layout.addWidget(chip)
        self._chip = chip
        self._hint.setVisible(False)
        self.setVisible(True)

    def show_hint(self, message: str) -> None:
        self._hint.setText(message)
        self._hint.setVisible(True)
        self.setVisible(True)

    def _on_parse_state(self, attachment: DocumentAttachment, state: str) -> None:
        if self._chip is not None and getattr(self._chip, "attachment", None) is attachment:
            self._chip.set_state(state)
        self.attachment_parsed.emit(attachment, state)

    def on_ocr_progress(self, attachment: DocumentAttachment, completed: int, total: int) -> None:
        if self._chip is not None and getattr(self._chip, "attachment", None) is attachment:
            self._chip.set_ocr_progress(completed, total)

    def on_ocr_page(self, attachment: DocumentAttachment, page: int) -> None:
        if self._chip is not None and getattr(self._chip, "attachment", None) is attachment:
            self._chip.set_ocr_page(page)


__all__ = ["ComposerAttachments", "mime_has_attachment", "parse_document_async"]
