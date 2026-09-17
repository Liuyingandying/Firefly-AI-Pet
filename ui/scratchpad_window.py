"""Scratchpad v1 — the temporary notebook window (临时记事本).

A compact, read-mostly inbox for things the user hands to Firefly:
time-grouped cards (今天 / 昨天 / YYYY-MM-DD), text preview with
click-to-expand, image thumbnails with click-to-preview, copy / delete,
a manual "+ 记一条" input, and optional Ctrl+V.  Nothing here ever writes
to Memory, Conversation, or Learning stores.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.scratchpad.models import ScratchpadItem, ScratchpadItemType
from ui.scratchpad_drop import ScratchpadDropController
from ui import theme

THUMBNAIL_HEIGHT = 72
PREVIEW_MAX_CHARS = 120
_THUMB_CACHE_MAX = 120


def _load_pixmap(path: Path) -> QPixmap | None:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image: QImage = reader.read()
    if image.isNull():
        return None
    pixmap = QPixmap.fromImage(
        image.scaledToHeight(
            theme.scaled_px(THUMBNAIL_HEIGHT), Qt.SmoothTransformation
        )
    )
    return pixmap if not pixmap.isNull() else None


class ScratchpadWindow(QWidget):
    """临时记事本 — never call it Memory; it is a temporary inbox."""

    def __init__(self, drop_controller: ScratchpadDropController, parent=None):
        super().__init__(parent)
        self._controller = drop_controller
        self._service = drop_controller._service
        self._thumb_cache: dict[str, QPixmap] = {}
        self._expanded: set[str] = set()

        self.setWindowTitle("临时记事本")
        self.resize(420, 560)
        self.setStyleSheet(
            f"QWidget {{ background: rgba{theme.V2.BACKGROUND}; color: rgba{theme.V2.TEXT_MAIN}; }}"
            f"QLabel {{ color: rgba{theme.V2.TEXT_MAIN}; background: transparent; }}"
            "QFrame { background: rgba(61,57,41,10); border-radius: 8px; }"
            "QPushButton { background: rgba(61,57,41,26); color: rgba{theme.V2.TEXT_MAIN};"
            " border: none; border-radius: 6px; padding: 2px 8px; font-size: 11pt; }"
            "QPushButton:hover { background: rgba(61,57,41,48); }"
            "QScrollArea { background: transparent; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("临时记事本")
        title.setStyleSheet(
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.scaled_px(15)}pt; font-weight: 700;"
        )
        header.addWidget(title)
        self._count_label = QLabel("0 条")
        self._count_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY}; font-size: {theme.scaled_px(11)}pt;"
        )
        header.addStretch(1)
        header.addWidget(self._count_label)
        self._add_button = QPushButton("+ 记一条")
        self._add_button.setCursor(Qt.PointingHandCursor)
        self._add_button.clicked.connect(self._on_manual_add)
        header.addWidget(self._add_button)
        root.addLayout(header)

        self._input = QTextEdit()
        self._input.setPlaceholderText("手动记一条（Ctrl+Enter 保存）…")
        self._input.setFixedHeight(64)
        self._input.hide()
        self._input.setStyleSheet(
            "QTextEdit { border: 1px solid rgba(120,140,150,80); border-radius: 6px;"
            " background: rgba(255,255,255,10); font-size: 12pt; }"
        )
        root.addWidget(self._input)
        shortcut = QShortcut(QKeySequence("Ctrl+Return"), self._input)
        shortcut.activated.connect(self._on_manual_confirm)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_host)
        root.addWidget(self._scroll, 1)

        self.refresh()

    # ------------------------------------------------------------- refresh

    def refresh(self) -> None:
        while self._list_layout.count() > 1:  # last child is the stretch
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        items = self._service.store.list_items()
        self._count_label.setText(f"{len(items)} 条")
        if not items:
            empty = QLabel("还没有临时记下什么。")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                f"color: rgba{theme.V2.TEXT_SECONDARY}; padding: 32px 0;"
            )
            self._list_layout.insertWidget(0, empty)
            return
        previous_group: str | None = None
        row_index = 0
        for item in items:
            group = self._group_label(item.created_at)
            if group != previous_group:
                header = QLabel(group)
                header.setStyleSheet(
                    f"color: rgba{theme.V2.TEXT_SECONDARY}; font-weight: 700;"
                    f" padding: 4px 2px 0 2px;"
                )
                self._list_layout.insertWidget(row_index, header)
                row_index += 1
                previous_group = group
            self._list_layout.insertWidget(row_index, self._build_card(item))
            row_index += 1

    # ------------------------------------------------------------- cards

    def _build_card(self, item: ScratchpadItem) -> QFrame:
        card = QFrame()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(4)

        head = QHBoxLayout()
        time_label = QLabel(datetime.fromtimestamp(item.created_at / 1000).strftime("%H:%M"))
        time_label.setStyleSheet(f"color: rgba{theme.V2.TEXT_SECONDARY};")
        head.addWidget(time_label)
        type_label = QLabel("文字" if item.item_type is ScratchpadItemType.TEXT else "图片")
        type_label.setStyleSheet(f"color: rgba{theme.V2.TEXT_SECONDARY};")
        head.addWidget(type_label)
        head.addStretch(1)
        for text, handler in self._card_actions(item):
            button = QPushButton(text)
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedHeight(24)
            button.clicked.connect(lambda _=False, fn=handler: fn())
            head.addWidget(button)
        layout.addLayout(head)

        if item.item_type is ScratchpadItemType.TEXT:
            expanded = item.id in self._expanded
            body_text = item.text if expanded else item.text[:PREVIEW_MAX_CHARS]
            body = QLabel(body_text)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if not expanded and len(item.text) > PREVIEW_MAX_CHARS:
                body.setText(item.text[:PREVIEW_MAX_CHARS] + " …")
                more = QLabel("点击展开全文")
                more.setStyleSheet(f"color: rgba{theme.CYAN_ACCENT};")
                more.setCursor(Qt.PointingHandCursor)
                more.mousePressEvent = lambda _e, item_id=item.id: self._toggle_expand(item_id)
                layout.addWidget(body)
                layout.addWidget(more)
            else:
                layout.addWidget(body)
            return card

        # image card
        path = self._service.store.asset_absolute_path(item)
        pixmap = self._thumbnail(path) if path is not None else None
        if pixmap is None:
            missing = QLabel("图片已不可用")
            missing.setAlignment(Qt.AlignCenter)
            missing.setFixedHeight(theme.scaled_px(THUMBNAIL_HEIGHT))
            missing.setStyleSheet(
                "color: rgba(150,150,150,180);"
                " background: rgba(255,255,255,8); border-radius: 6px;"
            )
            layout.addWidget(missing)
        else:
            thumb = QLabel()
            thumb.setPixmap(pixmap)
            thumb.setAlignment(Qt.AlignLeft)
            thumb.setCursor(Qt.PointingHandCursor)
            asset = path
            thumb.mousePressEvent = lambda _e, p=asset: self._preview_image(p)
            layout.addWidget(thumb)
        return card

    def _card_actions(self, item: ScratchpadItem):
        actions = [("复制", lambda item_id=item.id: self._on_copy(item_id))]
        if item.item_type is ScratchpadItemType.IMAGE:
            actions.append(
                ("打开文件夹", lambda item_id=item.id: self._on_reveal(item_id))
            )
        actions.append(("删除", lambda item_id=item.id: self._on_delete(item_id)))
        return actions

    # ------------------------------------------------------------ actions

    def _on_copy(self, item_id: str) -> None:
        item = self._service.store.get(item_id)
        if item is None:
            return
        clipboard = QApplication.clipboard()
        if item.item_type is ScratchpadItemType.TEXT:
            clipboard.setText(item.text)
        else:
            path = self._service.store.asset_absolute_path(item)
            if path is not None and path.is_file():
                clipboard.setImage(QImage(str(path)))
        self._flash_window_title("已复制")

    def _on_delete(self, item_id: str) -> None:
        item = self._service.store.get(item_id)
        if item is None:
            return
        kind = "这条文字" if item.item_type is ScratchpadItemType.TEXT else "这张图片"
        confirm = QMessageBox.question(
            self, "删除", f"确定删除{kind}吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._service.delete(item_id)
        self.refresh()

    def _on_reveal(self, item_id: str) -> None:
        item = self._service.store.get(item_id)
        if item is None:
            return
        path = self._service.store.asset_absolute_path(item)
        if path is None or not path.is_file():
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
        except OSError:
            pass

    def _on_manual_add(self) -> None:
        self._input.setVisible(not self._input.isVisible())
        if self._input.isVisible():
            self._input.setFocus()

    def _on_manual_confirm(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        outcome = self._controller.ingest_text(text)
        if outcome.ok:
            self._input.clear()
            self._input.hide()
            self.refresh()
        else:
            QMessageBox.information(self, "临时记事本", outcome.message)

    def _toggle_expand(self, item_id: str) -> None:
        if item_id in self._expanded:
            self._expanded.discard(item_id)
        else:
            self._expanded.add(item_id)
        self.refresh()

    def _preview_image(self, path: Path) -> None:
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            QMessageBox.information(self, "临时记事本", "图片已不可用")
            return
        preview = QLabel()
        preview.setPixmap(
            QPixmap.fromImage(image).scaled(
                900, 640, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )
        preview.setWindowFlag(Qt.Window)
        preview.setWindowTitle("临时记事本 · 预览")
        preview.show()

    def keyPressEvent(self, event) -> None:
        # Optional convenience: Ctrl+V inside the window captures text or an
        # image.  There is deliberately NO background clipboard monitoring.
        modifiers = event.modifiers()
        if event.matches(QKeySequence.Paste) or (
            modifiers & Qt.ControlModifier and event.key() == Qt.Key_V
        ):
            mime = QApplication.clipboard().mimeData()
            if mime is not None and (mime.hasText() or mime.hasImage()):
                outcome = self._controller.handle(mime)
                if outcome.ok:
                    self.refresh()
                    return
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ helpers

    def _thumbnail(self, path: Path) -> QPixmap | None:
        key = f"{path}|{int(path.stat().st_mtime) if path.exists() else 0}"
        cached = self._thumb_cache.get(key)
        if cached is not None:
            return cached
        pixmap = _load_pixmap(path)
        if pixmap is None:
            return None
        if len(self._thumb_cache) >= _THUMB_CACHE_MAX:
            self._thumb_cache.clear()
        self._thumb_cache[key] = pixmap
        return pixmap

    @staticmethod
    def _group_label(created_at_ms: int) -> str:
        moment = datetime.fromtimestamp(created_at_ms / 1000)
        today = datetime.now().date()
        date = moment.date()
        if date == today:
            return "今天"
        if (today - date).days == 1:
            return "昨天"
        return date.strftime("%Y-%m-%d")

    def _flash_window_title(self, text: str) -> None:
        base = "临时记事本"
        self.setWindowTitle(f"{base} · {text}")
        QTimer.singleShot(1500, lambda: self.setWindowTitle(base))
