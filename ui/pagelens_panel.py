"""PageLens overlay panel - Phase 9B real interaction.

A glass-surface persistent reading panel for PageLens concept cards.
Phase 9B: connected to browser PageLens via WebSocket bridge.

Signals (for bridge connection):
    related_requested(str): user clicked a related concept chip
    question_requested(str): user clicked a continue exploration question
    concept_requested(str): user clicked a top concept chip
    back_requested(): user clicked the back button
    close_requested(): user clicked the close button (×)

Architecture:
    - Reading dimensions FIXED logical pixels, independent of Pet scale
    - Header shows connection status (Connected/Offline)
    - Top concepts area shows page-level concept chips
    - Concept Card with real data from browser
    - Question View with SSE streaming
    - Back button for navigation
    - Non-opaque reading surface for readability
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QSize, QTimer, Signal, Slot, QThread
from PySide6.QtGui import QPainter, QPainterPath, QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QLayout,
    QScrollArea,
    QApplication,
    QSizePolicy,
    QFileDialog,
    QTextBrowser,
)

from . import theme
from .image_ocr_worker import ImageOcrSignalRelay, ImageOcrWorker

log = logging.getLogger("firefly.pagelens")

# ---------------------------------------------------------------------------
# PageLens uses a FIXED reading scale that does NOT follow Pet character scale.
# ---------------------------------------------------------------------------
PAGELENS_READING_SCALE = 1.0


def _pl_scaled(value: int | float) -> int:
    return int(round(value * PAGELENS_READING_SCALE))


def _pl_scaled_px(value: int | float) -> int:
    return max(1, _pl_scaled(value))


def _pl_font_px(value: int | float) -> int:
    return max(9, _pl_scaled(value))


class _FlowLayout(QLayout):
    """Compact wrapping layout used by PageLens concept chips."""

    def __init__(self, parent=None, *, horizontal_spacing: int = 5, vertical_spacing: int = 5):
        super().__init__(parent)
        self._items = []
        self._horizontal_spacing = horizontal_spacing
        self._vertical_spacing = vertical_spacing

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:
        return True

    def expandingDirections(self):
        return Qt.Orientations()

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        width = self.geometry().width()
        if width <= 0:
            width = _pl_scaled_px(theme.PAGELENS_WIDTH - 32)
        return QSize(width, self.heightForWidth(width))

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            if item.isEmpty():
                continue
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            if item.isEmpty():
                continue
            size = item.sizeHint()
            next_x = x + size.width() + self._horizontal_spacing
            if line_height > 0 and next_x - self._horizontal_spacing > effective.right() + 1:
                x = effective.x()
                y += line_height + self._vertical_spacing
                next_x = x + size.width() + self._horizontal_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), size))
            x = next_x
            line_height = max(line_height, size.height())

        return y + line_height - rect.y() + margins.bottom()


class PageLensPanel(QWidget):
    """A glass-surface persistent reading panel for PageLens concept cards.

    Phase 9B: connected to browser PageLens via WebSocket bridge.
    """

    # Signals for bridge connection
    related_requested = Signal(str)
    question_requested = Signal(str)
    concept_requested = Signal(str)
    back_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly PageLens")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())

        w = _pl_scaled_px(theme.PAGELENS_WIDTH)
        h = _pl_scaled_px(theme.PAGELENS_HEIGHT)
        self._default_height = h
        self.setFixedWidth(w)
        self.resize(w, h)
        self._apply_height_limits()

        # State (initialized before any method that reads them)
        self._visible = False
        self._anchor_side = "left"
        self._bridge_connected = False

        # Image attachment state (Stage 1A)
        self._image_path: str | None = None
        self._ocr_text: str = ""
        self._ocr_status: str = "未识别"
        self._ocr_generation: int = 0
        self._ocr_thread: QThread | None = None
        self._ocr_worker: ImageOcrWorker | None = None
        self._ocr_relay = ImageOcrSignalRelay(self)
        self._ocr_relay.finished.connect(self._on_ocr_finished)
        self._ocr_relay.error.connect(self._on_ocr_error)

        # Root layout with shadow
        root = QVBoxLayout(self)
        shadow = theme.SHADOW_MARGIN - 2
        root.setContentsMargins(shadow, shadow, shadow, shadow)
        root.setSpacing(0)

        self._glass = theme.GlassPanel(theme.RADIUS_CARD, self)
        theme.apply_soft_shadow(self._glass)
        root.addWidget(self._glass)

        # Inner layout
        inner = QVBoxLayout(self._glass)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        # Header
        self._header = self._build_header()
        inner.addWidget(self._header)

        # Image attachment toolbar (Stage 1A)
        self._image_toolbar = self._build_image_toolbar()
        inner.addWidget(self._image_toolbar)

        # Standalone collapsible OCR text panel (between toolbar and concepts)
        self._ocr_text_panel = QFrame(self._glass)
        self._ocr_text_panel.setVisible(False)
        self._ocr_text_panel.setMaximumHeight(_pl_scaled_px(220))
        self._ocr_text_panel.setStyleSheet(
            f"background: {theme.css_color(theme.GLASS_BACKGROUND_HOVER)}; "
            f"border: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-radius: {_pl_scaled_px(6)}px;"
        )
        ocr_inner = QVBoxLayout(self._ocr_text_panel)
        ocr_inner.setContentsMargins(_pl_scaled_px(8), _pl_scaled_px(6), _pl_scaled_px(8), _pl_scaled_px(6))
        ocr_inner.setSpacing(_pl_scaled_px(2))

        # Scrollable read-only text browser
        self._ocr_text_browser = QTextBrowser(self._ocr_text_panel)
        self._ocr_text_browser.setReadOnly(True)
        self._ocr_text_browser.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(9)}pt; "
            f"background: transparent; "
            f"border: none;"
        )
        self._ocr_text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._ocr_text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ocr_inner.addWidget(self._ocr_text_browser, 1)

        inner.addWidget(self._ocr_text_panel)

        # Compact context bar: concepts support wrapping without taking over the panel.
        self._top_concepts = self._build_top_concepts()
        inner.addWidget(self._top_concepts)

        # Separator
        sep = QFrame(self._glass)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {theme.css_color(theme.GLASS_BORDER)};")
        inner.addWidget(sep)

        # Scroll area for concept card / question view
        self._scroll = QScrollArea(self._glass)
        self._scroll.setWidgetResizable(True)
        self._scroll.setMinimumHeight(0)
        self._scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            f"QScrollBar:vertical {{ background: transparent; width: {_pl_scaled_px(6)}px; }}"
            f"QScrollBar::handle:vertical {{ background: {theme.css_color(theme.GLASS_BORDER)}; border-radius: 3px; min-height: {_pl_scaled_px(20)}px; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )
        inner.addWidget(self._scroll)

        # Content (concept card or question view)
        self._content = _ScrollContent(self._glass, self)
        self._scroll.setWidget(self._content)

        log.info("[PageLensPanel] created")

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _build_header(self) -> QFrame:
        header = QFrame(self._glass)
        header.setFixedHeight(_pl_scaled_px(theme.PAGELENS_HEADER_HEIGHT))
        header.setObjectName("pageLensHeader")

        layout = QHBoxLayout(header)
        layout.setContentsMargins(
            _pl_scaled_px(18), _pl_scaled_px(8),
            _pl_scaled_px(18), _pl_scaled_px(8),
        )
        layout.setSpacing(_pl_scaled_px(14))

        # Title
        title = QLabel("Firefly PageLens", header)
        title.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(11)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        layout.addWidget(title, 1)

        # Status badge
        self._badge = QFrame(header)
        self._badge.setFixedSize(_pl_scaled_px(64), _pl_scaled_px(20))
        self._badge.setObjectName("pageLensStatusBadge")
        self._badge_layout = QHBoxLayout(self._badge)
        self._badge_layout.setContentsMargins(0, 0, 0, 0)
        self._badge_layout.setAlignment(Qt.AlignCenter)
        self._badge_text = QLabel(self._badge)
        self._badge_layout.addWidget(self._badge_text)
        self._update_status_badge()
        layout.addWidget(self._badge)

        # Close button (×)
        self._close_btn = _CloseButton(header)
        self._close_btn.clicked.connect(lambda: self.close_requested.emit())
        layout.addWidget(self._close_btn)

        return header

    def _update_status_badge(self) -> None:
        if self._bridge_connected:
            text = "Connected"
            color = theme.CYAN_ACCENT
            bg_alpha = 32
        else:
            text = "Offline"
            color = (128, 128, 136, 255)  # gray with full alpha
            bg_alpha = 20

        self._badge_text.setText(text)
        self._badge_text.setStyleSheet(
            f"color: {theme.css_color(color)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(7)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._badge.setStyleSheet(
            f"background-color: {theme.css_color((*color[:3], bg_alpha))}; "
            f"border-radius: 10px;"
        )

    def set_bridge_connected(self, connected: bool) -> None:
        self._bridge_connected = connected
        self._update_status_badge()

    # ------------------------------------------------------------------
    # Image attachment (Stage 1A)
    # ------------------------------------------------------------------

    def _build_image_toolbar(self) -> QFrame:
        """Build the image attachment toolbar between header and OCR panel."""
        toolbar = QFrame(self._glass)
        toolbar.setFixedHeight(_pl_scaled_px(44))
        toolbar.setObjectName("pageLensImageToolbar")

        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(_pl_scaled_px(10), _pl_scaled_px(6), _pl_scaled_px(10), _pl_scaled_px(6))
        layout.setSpacing(_pl_scaled_px(8))

        # Add image button
        self._add_img_btn = _PlIconButton("📷", "添加图片", self._glass)
        self._add_img_btn.clicked.connect(self._on_add_image)
        layout.addWidget(self._add_img_btn)

        # Image preview (hidden until attached)
        self._img_preview = QLabel(self._glass)
        self._img_preview.setFixedSize(_pl_scaled_px(32), _pl_scaled_px(32))
        self._img_preview.setVisible(False)
        layout.addWidget(self._img_preview)

        # Filename label (elided, hidden until attached)
        self._img_filename = QLabel("", toolbar)
        self._img_filename.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(8)}pt;"
        )
        self._img_filename.setVisible(False)
        self._img_filename.setWordWrap(False)
        self._img_filename.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred,
        )
        layout.addWidget(self._img_filename)

        # Spacer
        layout.addStretch(1)

        # OCR status label
        self._ocr_status_label = QLabel("未识别", toolbar)
        self._ocr_status_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(8)}pt;"
        )
        self._ocr_status_label.setVisible(False)
        layout.addWidget(self._ocr_status_label)

        # Toggle OCR text button (hidden until OCR succeeds)
        self._ocr_view_btn = _PlIconButton("🔍", "查看 OCR", self._glass)
        self._ocr_view_btn.setVisible(False)
        self._ocr_view_btn.clicked.connect(self._on_toggle_ocr)
        layout.addWidget(self._ocr_view_btn)

        # Remove image button (hidden until attached)
        self._remove_img_btn = _PlIconButton("✕", "移除", self._glass)
        self._remove_img_btn.setVisible(False)
        self._remove_img_btn.clicked.connect(self._on_remove_image)
        layout.addWidget(self._remove_img_btn)

        return toolbar

    def _on_add_image(self) -> None:
        """Open file dialog to attach an image for local OCR."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.webp)",
        )
        if not path:
            return
        self._attach_image(str(Path(path).resolve()))

    def _attach_image(self, image_path: str) -> None:
        """Attach an image and start OCR. Replaces any previously attached image."""
        # Cancel any in-flight OCR
        self._cancel_ocr()
        self._image_path = image_path
        self._ocr_text = ""
        self._ocr_status = "识别中"
        self._ocr_generation += 1
        gen = self._ocr_generation

        # Show preview + filename
        pixmap = QPixmap(image_path)
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                _pl_scaled_px(32), _pl_scaled_px(32),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            self._img_preview.setPixmap(scaled)
            self._img_preview.setVisible(True)

        # Elide filename
        name = Path(image_path).name
        metrics = QFontMetrics(self.font())
        max_width = self.width() - _pl_scaled_px(200)
        if max_width > 0:
            display_name = metrics.elidedText(name, Qt.ElideMiddle, max_width)
        else:
            display_name = name
        self._img_filename.setText(display_name)
        self._img_filename.setVisible(True)

        # Show remove button
        self._remove_img_btn.setVisible(True)

        # Show status
        self._ocr_status_label.setVisible(True)
        self._update_ocr_status()

        # Hide OCR text panel
        self._ocr_text_panel.setVisible(False)
        self._ocr_view_btn.setVisible(False)

        # Run OCR in background
        thread = QThread(self)
        worker = ImageOcrWorker(image_path, generation=gen)
        worker.moveToThread(thread)

        # PySide Python callables must not rely on AutoConnection here: an
        # explicit queued connection is the native-crash safety boundary for
        # every QWidget update below.
        worker.finished.connect(
            self._ocr_relay.forward_finished, Qt.QueuedConnection,
        )
        worker.error.connect(
            self._ocr_relay.forward_error, Qt.QueuedConnection,
        )
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)

        thread.started.connect(worker.run)
        thread.start()

        self._ocr_thread = thread
        self._ocr_worker = worker

    @Slot(int, str)
    def _on_ocr_finished(self, gen: int, ocr_text: str) -> None:
        """Handle OCR completion (runs in Qt main thread via QueuedConnection)."""
        if gen != self._ocr_generation:
            # Stale result from an old image; ignore.
            return
        self._ocr_worker = None
        self._ocr_text = ocr_text
        self._ocr_status = "已识别" if ocr_text else "OCR失败"
        self._update_ocr_status()

        if ocr_text:
            # Set text but do NOT auto-expand — user must click "查看 OCR"
            self._ocr_text_browser.setPlainText(ocr_text)
            self._ocr_view_btn.setVisible(True)

    @Slot(int, str)
    def _on_ocr_error(self, gen: int, message: str) -> None:
        """Handle OCR error (runs in Qt main thread via QueuedConnection)."""
        if gen != self._ocr_generation:
            return
        self._ocr_worker = None
        self._ocr_status = "OCR失败"
        self._update_ocr_status()
        log.warning("[PageLensPanel] OCR error gen=%d: %s", gen, message)

    def _update_ocr_status(self) -> None:
        status_colors = {
            "未识别": theme.TEXT_SECONDARY,
            "识别中": theme.CLAUDE_ORANGE,
            "已识别": theme.CHATGPT_GREEN,
            "OCR失败": theme.ERROR_STATUS,
        }
        color = status_colors.get(self._ocr_status, theme.TEXT_SECONDARY)
        self._ocr_status_label.setStyleSheet(
            f"color: {theme.css_color(color)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(8)}pt;"
        )
        self._ocr_status_label.setText(self._ocr_status)

    def _on_remove_image(self) -> None:
        """Remove the attached image and clear OCR state."""
        # Invalidate any result already queued by the current worker.
        self._ocr_generation += 1
        self._cancel_ocr()
        self._image_path = None
        self._ocr_text = ""
        self._ocr_status = "未识别"
        self._img_preview.setVisible(False)
        self._img_filename.setVisible(False)
        self._img_filename.setText("")
        self._remove_img_btn.setVisible(False)
        self._ocr_status_label.setVisible(False)
        self._ocr_text_panel.setVisible(False)
        self._ocr_view_btn.setVisible(False)
        self._ocr_text_browser.setPlainText("")

    def _on_toggle_ocr(self) -> None:
        """Toggle the collapsible OCR text panel visibility."""
        if self._ocr_text_panel.isHidden():
            self._ocr_text_panel.setVisible(True)
            self._ocr_view_btn._icon = "收起"
            self._ocr_view_btn.setToolTip("收起 OCR")
        else:
            self._ocr_text_panel.setVisible(False)
            self._ocr_view_btn._icon = "🔍"
            self._ocr_view_btn.setToolTip("查看 OCR")
        self._ocr_view_btn.update()

    def _cancel_ocr(self) -> None:
        """Stop accepting the current job and ask its event loop to exit."""
        self._ocr_worker = None
        if self._ocr_thread is not None:
            thread = self._ocr_thread
            self._ocr_thread = None
            try:
                if thread.isRunning():
                    # RapidOCR itself is not interruptible.  quit() takes effect
                    # as soon as worker.run() returns; its result is generation-
                    # guarded and the normal finished/error wiring owns cleanup.
                    thread.quit()
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # Top concepts area
    # ------------------------------------------------------------------

    def _build_top_concepts(self) -> QFrame:
        frame = QFrame(self._glass)
        frame.setObjectName("pageLensTopConcepts")
        frame.setStyleSheet(
            f"QFrame#pageLensTopConcepts {{ "
            f"background-color: {theme.css_color(theme.GLASS_BACKGROUND_SELECTED)}; "
            f"border-top: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-bottom: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            "}"
        )
        context_height = max(70, min(_pl_scaled_px(82), 90))
        frame.setFixedHeight(context_height)

        layout = QHBoxLayout(frame)
        layout.setContentsMargins(
            _pl_scaled_px(14), _pl_scaled_px(8),
            _pl_scaled_px(10), _pl_scaled_px(8),
        )
        layout.setSpacing(_pl_scaled_px(8))

        # Title
        self._top_concepts_header = QLabel("本页概念", frame)
        self._top_concepts_header.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(8)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._top_concepts_header.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._top_concepts_header.setFixedWidth(_pl_scaled_px(56))
        layout.addWidget(self._top_concepts_header, 0, Qt.AlignTop)

        # Chips viewport: wrapping is preserved, overflow scrolls inside the bar.
        self._top_concepts_scroll = QScrollArea(frame)
        self._top_concepts_scroll.setWidgetResizable(True)
        self._top_concepts_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._top_concepts_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._top_concepts_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            f"QScrollBar:vertical {{ background: transparent; width: {_pl_scaled_px(4)}px; }}"
            f"QScrollBar::handle:vertical {{ background: {theme.css_color(theme.GLASS_BORDER)}; border-radius: 2px; min-height: {_pl_scaled_px(14)}px; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )
        self._top_concepts_content = QWidget(self._top_concepts_scroll)
        self._top_concepts_content.setStyleSheet("background: transparent;")
        self._top_concepts_layout = _FlowLayout(
            self._top_concepts_content,
            horizontal_spacing=_pl_scaled_px(5),
            vertical_spacing=_pl_scaled_px(5),
        )
        self._top_concepts_layout.setContentsMargins(0, 0, 0, 0)
        self._top_concept_widgets: list[QWidget] = []
        self._top_concepts_scroll.setWidget(self._top_concepts_content)
        layout.addWidget(self._top_concepts_scroll, 1)

        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return frame

    def set_top_concepts(self, items: list[str]) -> None:
        """Render top concept chips. Clicking sends concept_requested signal."""
        # Clear old chips
        for widget in self._top_concept_widgets:
            widget.deleteLater()
        self._top_concept_widgets.clear()
        while self._top_concepts_layout.count():
            self._top_concepts_layout.takeAt(0)

        # Show at most 8 concepts
        for text in (items or [])[:8]:
            chip = _TopConceptChip(text, self)
            chip.clicked.connect(lambda t=text: self.concept_requested.emit(t))
            self._top_concepts_layout.addWidget(chip)
            self._top_concept_widgets.append(chip)
        self._top_concepts_content.updateGeometry()
        self._top_concepts_scroll.verticalScrollBar().setValue(0)
        self._top_concepts.updateGeometry()
        self._request_height_fit()

    def _screen_height_limit(self) -> int:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return self._default_height
        return max(_pl_scaled_px(320), int(screen.availableGeometry().height() * 0.85))

    def _apply_height_limits(self) -> None:
        maximum = self._screen_height_limit()
        self.setMinimumHeight(min(self._default_height, maximum))
        self.setMaximumHeight(maximum)

    def _request_height_fit(self) -> None:
        QTimer.singleShot(0, self._fit_height_to_content)

    def _fit_height_to_content(self) -> None:
        if not hasattr(self, "_content"):
            return
        self._apply_height_limits()
        self._content.layout().activate()
        self._top_concepts.layout().activate()

        root_margins = self.layout().contentsMargins()
        natural_height = (
            root_margins.top()
            + root_margins.bottom()
            + self._header.sizeHint().height()
            + self._image_toolbar.sizeHint().height()
        )

        # Include OCR panel height only when visible
        if hasattr(self, "_ocr_text_panel") and not self._ocr_text_panel.isHidden():
            natural_height += self._ocr_text_panel.sizeHint().height()

        natural_height += (
            1
            + self._top_concepts.sizeHint().height()
            + 1
            + self._content.sizeHint().height()
        )
        target_height = min(
            max(self.minimumHeight(), natural_height),
            self.maximumHeight(),
        )
        if target_height == self.height():
            return

        old_center_y = self.frameGeometry().center().y()
        self.resize(self.width(), target_height)
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            target_y = old_center_y - target_height // 2
            target_y = max(available.top(), min(target_y, available.bottom() - target_height + 1))
            self.move(self.x(), target_y)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_concept(self, card: dict) -> None:
        self._content.set_concept(card)
        self._request_height_fit()

    def show_loading(self, term: str) -> None:
        self._content.show_loading(term)
        self._request_height_fit()

    def show_error(self, message: str) -> None:
        self._content.show_error(message)
        self._request_height_fit()

    def show_panel(self) -> None:
        self._visible = True
        self._request_height_fit()
        self.show()
        self.raise_()
        log.info("[PageLensPanel] show")

    def hide_panel(self) -> None:
        self._visible = False
        self.hide()
        log.info("[PageLensPanel] hide")

    def toggle(self) -> bool:
        if self._visible:
            self.hide_panel()
            return False
        else:
            self.show_panel()
            return True

    def apply_scale(self) -> None:
        """Phase 9A.1/9B: FIXED reading scale — ignore Pet character scale."""
        pass

    # -- Question view delegation (bridge / app → _content) ----------------

    def show_question_loading(self, parent_term: str, question: str) -> None:
        self._content.show_question_loading(parent_term, question)
        self._request_height_fit()

    def append_question_delta(self, delta: str) -> None:
        self._content.append_question_delta(delta)
        self._request_height_fit()

    def finish_question(self) -> None:
        self._content.finish_question()
        self._request_height_fit()

    def show_question_error(self, message: str) -> None:
        self._content.show_question_error(message)
        self._request_height_fit()

    # -- Read-only introspection (needed by tests and bridge wiring) --------

    @property
    def related_widgets(self) -> list:
        return self._content._related_widgets

    @property
    def question_widgets(self) -> list:
        return self._content._question_widgets

    @property
    def question_parent_label(self) -> QLabel:
        return self._content._question_parent_label

    @property
    def question_title_label(self) -> QLabel:
        return self._content._question_title_label

    @property
    def question_status_label(self) -> QLabel:
        return self._content._question_status_label

    @property
    def question_answer_label(self) -> QLabel:
        return self._content._question_answer_label

    @property
    def bridge_connected(self) -> bool:
        return self._bridge_connected

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def anchor_side(self) -> str:
        return self._anchor_side

    def set_anchor_side(self, side: str) -> None:
        if side not in ("left", "right"):
            side = "left"
        if side != self._anchor_side:
            self._anchor_side = side
            log.info("[PageLensPanel] anchor side: %s", side)


# ---------------------------------------------------------------------------
# Top concept chip (for "本页概念" area)
# ---------------------------------------------------------------------------

class _TopConceptChip(QFrame):
    """Compact chip for top page concepts."""

    clicked = Signal(str)

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = text
        self.setFixedHeight(_pl_scaled_px(22))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setToolTip(text)
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False
        self.setObjectName("pageLensTopConceptChip")

    def sizeHint(self) -> QSize:
        text_width = QFontMetrics(self.font()).horizontalAdvance(self._text)
        width = max(_pl_scaled_px(48), min(text_width + _pl_scaled_px(20), _pl_scaled_px(180)))
        return QSize(width, _pl_scaled_px(22))

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._text)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 11, 11)

        if self._hovered:
            painter.fillPath(path, theme.qcolor(theme.GLASS_BACKGROUND_HOVER))
            painter.setPen(theme.qcolor(theme.CYAN_ACCENT))
        else:
            painter.fillPath(path, theme.qcolor(theme.GLASS_BACKGROUND))
            painter.setPen(theme.qcolor(theme.GLASS_BORDER))

        painter.drawPath(path)
        painter.setPen(theme.qcolor(theme.TEXT_SECONDARY))
        text_rect = self.rect().adjusted(_pl_scaled_px(8), 0, -_pl_scaled_px(8), 0)
        text = QFontMetrics(painter.font()).elidedText(
            self._text, Qt.ElideRight, text_rect.width()
        )
        painter.drawText(text_rect, Qt.AlignCenter, text)
        painter.end()


# ---------------------------------------------------------------------------
# Scroll content (concept card + question view)
# ---------------------------------------------------------------------------

class _ScrollContent(QWidget):
    """Scrollable inner widget that holds the PageLens concept card content."""

    def __init__(self, parent: QWidget | None = None, parent_panel: PageLensPanel | None = None):
        super().__init__(parent)
        # Hold references to parent panel's signals for forwarding clicks
        self._parent_panel = parent_panel
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(
            _pl_scaled_px(16), _pl_scaled_px(14),
            _pl_scaled_px(16), _pl_scaled_px(18),
        )
        self._layout.setSpacing(_pl_scaled_px(12))
        self._layout.setAlignment(Qt.AlignTop)

        # --- Title card: back action, bilingual title, divider ---
        self._title_card = QFrame(self)
        self._title_card.setObjectName("pageLensConceptTitleCard")
        self._title_card.setStyleSheet(
            f"QFrame#pageLensConceptTitleCard {{ "
            f"background-color: {theme.css_color(theme.GLASS_BACKGROUND_HOVER)}; "
            f"border: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-radius: {_pl_scaled_px(theme.RADIUS_ITEM)}px; "
            "}"
        )
        title_layout = QVBoxLayout(self._title_card)
        title_layout.setContentsMargins(
            _pl_scaled_px(14), _pl_scaled_px(9),
            _pl_scaled_px(14), _pl_scaled_px(12),
        )
        title_layout.setSpacing(_pl_scaled_px(5))

        # --- Back button (hidden by default) ---
        self._back_btn = _BackButton(self._title_card)
        self._back_btn.hidden = True
        self._back_btn.clicked.connect(lambda: None)  # placeholder
        title_layout.addWidget(self._back_btn)

        # --- Term label ---
        self._term_label = QLabel(self._title_card)
        self._term_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(16)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._term_label.setWordWrap(True)
        self._term_label.setMinimumHeight(_pl_scaled_px(26))
        self._term_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        title_layout.addWidget(self._term_label)

        # --- English subtitle ---
        self._english_label = QLabel(self._title_card)
        self._english_label.setStyleSheet(
            f"color: {theme.css_color(theme.CYAN_ACCENT)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(11)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._english_label.setWordWrap(True)
        self._english_label.setMinimumHeight(_pl_scaled_px(18))
        self._english_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        title_layout.addWidget(self._english_label)

        # --- Separator ---
        sep = QFrame(self._title_card)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {theme.css_color(theme.GLASS_BORDER)};")
        title_layout.addWidget(sep)
        self._layout.addWidget(self._title_card)

        # --- Summary ---
        self._summary_label = QLabel(self)
        self._summary_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(10.5)}pt; "
            f"line-height: 170%;"
        )
        self._summary_label.setContentsMargins(
            _pl_scaled_px(4), _pl_scaled_px(8),
            _pl_scaled_px(4), _pl_scaled_px(8),
        )
        self._summary_label.setWordWrap(True)
        self._summary_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred,
        )
        self._layout.addWidget(self._summary_label)

        # --- Separator ---
        sep2 = QFrame(self)
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f"background: {theme.css_color(theme.GLASS_BORDER)};")
        self._layout.addWidget(sep2)

        # --- Context section ---
        self._context_header = QLabel(self)
        self._context_header.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(9)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._context_header.setText("为什么这里提到它")
        self._context_header.setContentsMargins(
            _pl_scaled_px(2), _pl_scaled_px(4), 0, 0,
        )
        self._layout.addWidget(self._context_header)

        self._context_label = QLabel(self)
        self._context_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(10)}pt; "
            f"line-height: 170%;"
        )
        self._context_label.setContentsMargins(
            _pl_scaled_px(2), _pl_scaled_px(4),
            _pl_scaled_px(2), _pl_scaled_px(8),
        )
        self._context_label.setWordWrap(True)
        self._context_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred,
        )
        self._layout.addWidget(self._context_label)

        # --- Separator ---
        sep3 = QFrame(self)
        sep3.setFixedHeight(1)
        sep3.setStyleSheet(f"background: {theme.css_color(theme.GLASS_BORDER)};")
        self._layout.addWidget(sep3)

        # --- Related concepts header ---
        self._related_header = QLabel(self)
        self._related_header.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(9)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._related_header.setText("相关概念")
        self._related_header.setContentsMargins(
            _pl_scaled_px(2), _pl_scaled_px(4), 0, 0,
        )
        self._layout.addWidget(self._related_header)

        self._related_layout = _FlowLayout(
            horizontal_spacing=_pl_scaled_px(7),
            vertical_spacing=_pl_scaled_px(7),
        )
        self._related_widgets: list[QWidget] = []
        self._layout.addLayout(self._related_layout)

        # --- Separator ---
        sep4 = QFrame(self)
        sep4.setFixedHeight(1)
        sep4.setStyleSheet(f"background: {theme.css_color(theme.GLASS_BORDER)};")
        self._layout.addWidget(sep4)

        # --- Questions header ---
        self._questions_header = QLabel(self)
        self._questions_header.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(9)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._questions_header.setText("继续探索")
        self._questions_header.setContentsMargins(
            _pl_scaled_px(2), _pl_scaled_px(4), 0, 0,
        )
        self._layout.addWidget(self._questions_header)

        self._questions_layout = QVBoxLayout()
        self._questions_layout.setSpacing(_pl_scaled_px(4))
        self._question_widgets: list[QWidget] = []
        self._layout.addLayout(self._questions_layout)

        self._concept_section_widgets = (
            self._summary_label,
            sep2,
            self._context_header,
            self._context_label,
            sep3,
            self._related_header,
            sep4,
            self._questions_header,
        )

        # --- Question view elements (hidden by default) ---
        self._question_parent_label = QLabel(self)
        self._question_parent_label.setStyleSheet(
            f"color: {theme.css_color(theme.CYAN_ACCENT)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(10)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._question_parent_label.setVisible(False)
        self._layout.addWidget(self._question_parent_label)

        self._question_title_label = QLabel(self)
        self._question_title_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(12)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        self._question_title_label.setWordWrap(True)
        self._question_title_label.setVisible(False)
        self._layout.addWidget(self._question_title_label)

        self._question_status_label = QLabel(self)
        self._question_status_label.setStyleSheet(
            f"color: {theme.css_color(theme.CYAN_ACCENT)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(9)}pt; "
        )
        self._question_status_label.setVisible(False)
        self._layout.addWidget(self._question_status_label)

        self._question_answer_label = QLabel(self)
        self._question_answer_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(10.5)}pt; "
            f"line-height: 170%;"
        )
        self._question_answer_label.setWordWrap(True)
        self._question_answer_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred,
        )
        self._question_answer_label.setVisible(False)
        self._layout.addWidget(self._question_answer_label)

        # --- Idle state ---
        self._idle_label = QLabel(self)
        self._idle_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(11)}pt; "
            f"font-style: italic;"
        )
        self._idle_label.setAlignment(Qt.AlignCenter)
        self._idle_label.setText("等待网页内容…")
        self._idle_label.setVisible(True)
        self._layout.addWidget(self._idle_label)

    # -- Concept card methods -------------------------------------------

    def set_concept(self, card: dict) -> None:
        self._show_concept_view()
        self._back_btn.set_text(card.get("_back_term", ""))
        self._term_label.setText(card.get("term", ""))
        self._english_label.setText(card.get("english", ""))
        self._summary_label.setText(card.get("summary", ""))
        self._context_label.setText(card.get("context", ""))

        # Related chips
        for w in self._related_widgets:
            w.deleteLater()
        self._related_widgets.clear()
        while self._related_layout.count():
            self._related_layout.takeAt(0)
        for text in card.get("related", []):
            chip = _RelatedChip(text, self)
            chip.clicked.connect(lambda t=text: self._emit_related(t))
            self._related_layout.addWidget(chip)
            self._related_widgets.append(chip)

        # Question items
        for item in self._questions_layout.children():
            if isinstance(item, QWidget) and item not in (
                self._questions_header, self._question_parent_label,
                self._question_title_label, self._question_status_label,
                self._question_answer_label, self._idle_label,
            ):
                item.deleteLater()
        for w in self._question_widgets:
            w.deleteLater()
        self._question_widgets.clear()
        for text in card.get("questions", []):
            qitem = _QuestionItem(text, self)
            qitem.clicked.connect(self._emit_question)
            self._questions_layout.addWidget(qitem)
            self._question_widgets.append(qitem)

    def show_loading(self, term: str) -> None:
        self._show_concept_view()
        self._back_btn.hidden = True
        self._term_label.setText(term)
        self._english_label.setText("")
        self._summary_label.setText("加载中...")
        self._context_label.setText("")
        for w in self._related_widgets:
            w.deleteLater()
        self._related_widgets.clear()
        while self._related_layout.count():
            self._related_layout.takeAt(0)
        for w in self._question_widgets:
            w.deleteLater()
        self._question_widgets.clear()

    def show_error(self, message: str) -> None:
        self._show_concept_view()
        self._back_btn.hidden = True
        self._term_label.setText("PageLens")
        self._english_label.setText("")
        self._summary_label.setText(message)
        self._context_label.setText("")
        for w in self._related_widgets:
            w.deleteLater()
        self._related_widgets.clear()
        while self._related_layout.count():
            self._related_layout.takeAt(0)
        for w in self._question_widgets:
            w.deleteLater()
        self._question_widgets.clear()

    # -- Question view methods ------------------------------------------

    def show_question_loading(self, parent_term: str, question: str) -> None:
        self._show_question_view()
        self._question_parent_label.setText(f"← {parent_term}")
        self._question_parent_label.setVisible(True)
        self._question_title_label.setText(question)
        self._question_title_label.setVisible(True)
        self._question_status_label.setText("Qwen 正在解释…")
        self._question_status_label.setVisible(True)
        self._question_answer_label.setText("")
        self._question_answer_label.setVisible(True)
        self._hide_concept_sections()
        self._refresh_question_answer_geometry()

    def append_question_delta(self, delta: str) -> None:
        current = self._question_answer_label.text()
        self._question_answer_label.setText(current + delta)
        self._refresh_question_answer_geometry()

    def finish_question(self) -> None:
        self._question_status_label.setText("完成")

    def show_question_error(self, message: str) -> None:
        self._show_question_view()
        self._question_status_label.setText("回答失败")
        self._question_answer_label.setText(message)
        self._question_answer_label.setVisible(True)
        self._refresh_question_answer_geometry()

    # -- Helpers --------------------------------------------------------

    def _show_concept_view(self) -> None:
        self._back_btn.hidden = False
        self._term_label.setVisible(True)
        self._english_label.setVisible(True)
        for widget in self._concept_section_widgets:
            widget.setVisible(True)
        for widget in (*self._related_widgets, *self._question_widgets):
            widget.setVisible(True)
        # Hide question view
        self._question_parent_label.setVisible(False)
        self._question_title_label.setVisible(False)
        self._question_status_label.setVisible(False)
        self._question_answer_label.setVisible(False)
        self._idle_label.setVisible(False)

    def _show_question_view(self) -> None:
        self._back_btn.hidden = False
        self._back_btn.set_text("")  # no back term for question view
        self._term_label.setVisible(False)
        self._english_label.setVisible(False)
        self._hide_concept_sections()
        self._idle_label.setVisible(False)

    def _refresh_question_answer_geometry(self) -> None:
        """Propagate the wrapped answer height through the scroll hierarchy."""
        answer = self._question_answer_label
        layout_margins = self._layout.contentsMargins()
        available_width = answer.width()
        if available_width <= 0:
            available_width = max(
                1,
                self.width() - layout_margins.left() - layout_margins.right(),
            )

        required_height = answer.heightForWidth(available_width) if answer.text() else 0
        answer.setMinimumHeight(max(0, required_height))
        answer.updateGeometry()

        self._layout.invalidate()
        self._layout.activate()
        self.adjustSize()
        self.updateGeometry()

        panel = self._parent_panel
        if panel is not None:
            panel._scroll.widget().updateGeometry()
            panel._scroll.viewport().updateGeometry()
            panel._scroll.viewport().update()
            panel._scroll.verticalScrollBar().updateGeometry()

    def _hide_concept_sections(self) -> None:
        """Hide all concept card sections (summary, context, related, questions)."""
        self._term_label.setVisible(False)
        self._english_label.setVisible(False)
        for widget in self._concept_section_widgets:
            widget.setVisible(False)
        for widget in (*self._related_widgets, *self._question_widgets):
            widget.setVisible(False)

    def _emit_related(self, text: str) -> None:
        log.info("[PageLensPanel] related: %s", text)
        if self._parent_panel is not None:
            self._parent_panel.related_requested.emit(text)

    def _emit_question(self, text: str) -> None:
        log.info("[PageLensPanel] question: %s", text)
        if self._parent_panel is not None:
            self._parent_panel.question_requested.emit(text)


# ---------------------------------------------------------------------------
# Back button
# ---------------------------------------------------------------------------

class _BackButton(QFrame):
    """A small back button (← term)."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = ""
        self._hovered = False
        self.hidden = True
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(_pl_scaled_px(26))

    def set_text(self, term: str) -> None:
        self._text = term
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        if self.hidden:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self._hovered:
            painter.setPen(theme.qcolor(theme.CYAN_ACCENT))
        else:
            painter.setPen(theme.qcolor(theme.TEXT_SECONDARY))
        painter.drawText(6, self.height() // 2 + 4, "← " + self._text)
        painter.end()


# ---------------------------------------------------------------------------
# Related chip
# ---------------------------------------------------------------------------

class _RelatedChip(QFrame):
    """A small pill-shaped chip for related concepts."""

    clicked = Signal(str)

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = text
        self.setFixedHeight(_pl_scaled_px(26))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setToolTip(text)
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False
        self._selected = False
        self.setObjectName("pageLensRelatedChip")

    def sizeHint(self) -> QSize:
        text_width = QFontMetrics(self.font()).horizontalAdvance(self._text)
        width = max(_pl_scaled_px(56), min(text_width + _pl_scaled_px(22), _pl_scaled_px(200)))
        return QSize(width, _pl_scaled_px(26))

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._text)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 13, 13)

        if self._selected:
            painter.fillPath(path, theme.qcolor(theme.CYAN_ACCENT))
            painter.setPen(theme.qcolor(theme.TRANSPARENT))
        elif self._hovered:
            painter.fillPath(path, theme.qcolor(theme.GLASS_BACKGROUND_HOVER))
            painter.setPen(theme.qcolor(theme.CYAN_ACCENT))
        else:
            painter.fillPath(path, theme.qcolor(theme.GLASS_BACKGROUND))
            painter.setPen(theme.qcolor(theme.GLASS_BORDER))

        painter.drawPath(path)
        painter.setPen(theme.qcolor(theme.TEXT_PRIMARY if self._selected else theme.TEXT_SECONDARY))
        text_rect = self.rect().adjusted(_pl_scaled_px(8), 0, -_pl_scaled_px(8), 0)
        text = QFontMetrics(painter.font()).elidedText(
            self._text, Qt.ElideRight, text_rect.width()
        )
        painter.drawText(text_rect, Qt.AlignCenter, text)
        painter.end()


# ---------------------------------------------------------------------------
# Question item
# ---------------------------------------------------------------------------

class _QuestionItem(QFrame):
    """A clickable question row."""

    clicked = Signal(str)

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._text = text
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumHeight(_pl_scaled_px(32))
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False
        self.setObjectName("pageLensQuestionItem")

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        text_rect = QRect(0, 0, max(1, width - _pl_scaled_px(16)), 10000)
        bounds = QFontMetrics(self.font()).boundingRect(
            text_rect, Qt.TextWordWrap, "> " + self._text
        )
        return max(_pl_scaled_px(32), bounds.height() + _pl_scaled_px(12))

    def sizeHint(self) -> QSize:
        width = _pl_scaled_px(theme.PAGELENS_WIDTH - 40)
        return QSize(width, self.heightForWidth(width))

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            # Accept the press so a real click inside QScrollArea keeps this
            # widget as the mouse receiver through release.
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._text)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self._hovered:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), theme.RADIUS_ITEM, theme.RADIUS_ITEM)
            painter.fillPath(path, theme.qcolor(theme.GLASS_BACKGROUND_HOVER))

        painter.setPen(theme.qcolor(theme.TEXT_PRIMARY if self._hovered else theme.TEXT_SECONDARY))
        painter.drawText(
            self.rect().adjusted(_pl_scaled_px(8), _pl_scaled_px(4), -_pl_scaled_px(8), -_pl_scaled_px(4)),
            Qt.AlignVCenter | Qt.TextWordWrap,
            "> " + self._text,
        )
        painter.end()


# ---------------------------------------------------------------------------
# Image toolbar icon button
# ---------------------------------------------------------------------------

class _PlIconButton(QFrame):
    """Compact icon button for the image toolbar."""

    clicked = Signal()

    def __init__(self, icon: str, tooltip: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._icon = icon
        self._tooltip = tooltip
        self.setFixedSize(_pl_scaled_px(36), _pl_scaled_px(32))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self._hovered = False

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainter
        from PySide6.QtCore import QRect

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._hovered:
            painter.fillRect(
                self.rect(), theme.qcolor(theme.GLASS_BACKGROUND_HOVER)
            )
        else:
            painter.fillRect(self.rect(), Qt.transparent)

        painter.setPen(theme.qcolor(theme.TEXT_SECONDARY))
        painter.drawText(
            self.rect(), Qt.AlignCenter, self._icon
        )
        painter.end()


# ---------------------------------------------------------------------------
# Close button
# ---------------------------------------------------------------------------

class _CloseButton(QFrame):
    """Minimal close button (×) in the panel header."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._hovered = False
        self.setFixedSize(_pl_scaled_px(26), _pl_scaled_px(26))
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("pageLensCloseButton")
        self.setToolTip("关闭 PageLens")

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(theme.qcolor(theme.TEXT_SECONDARY if not self._hovered else theme.TEXT_PRIMARY))
        painter.drawText(self.rect(), Qt.AlignCenter, "×")
        painter.end()
