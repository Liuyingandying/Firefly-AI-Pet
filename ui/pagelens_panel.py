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
from enum import Enum
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QSize, QTimer, Signal, Slot, QThread, QEvent
from PySide6.QtGui import QPainter, QPainterPath, QFont, QFontMetrics, QPixmap, QColor, QPen, QCursor
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
    QPushButton,
)

from . import theme
from .image_ocr_worker import ImageOcrSignalRelay, ImageOcrWorker
from .image_vision_worker import ImageVisionSignalRelay, ImageVisionWorker

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


def _tier_opacity_of(widget: QWidget) -> float:
    """Tier opacity of the owning PageLensPanel (walks up the parent chain).

    Painter children (chips, question rows) read the live tier at paint time
    so their fills stay in sync with the translucent glass surface.
    """
    cursor: QWidget | None = widget
    while cursor is not None:
        if isinstance(cursor, PageLensPanel):
            return cursor.tier_opacity()
        cursor = cursor.parentWidget()
    return theme.PAGELENS_OPACITY_FOCUSED


class _ViewState(Enum):
    """Which body view the concept card area is showing."""

    VIEW_CONCEPT = "concept"
    VIEW_QUESTION = "question"
    # A related-concept detail card is on screen and the origin card is kept
    # locally so the back arrow can restore it without a bridge round-trip.
    VIEW_RELATED = "related"


class PageLensState(Enum):
    """Public reading-surface state used by the consolidated UI."""

    COMPACT = "compact"
    EXPLAIN = "explain"
    VISUAL_EXPLAIN = "visual_explain"


class _PageLensGlass(theme.GlassPanel):
    """PageLens glass surface with a three-tier readability opacity.

    The fill alpha follows the panel's current tier (idle / hover / focused)
    so the page behind shows through when the user is merely reading, while
    hover and window focus restore full clarity for interaction.
    """

    def set_tier_opacity(self, opacity: float) -> None:
        self._tier_opacity = opacity
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        opacity = getattr(self, "_tier_opacity", 1.0)
        fill = QColor(
            theme.GLASS_BACKGROUND[0],
            theme.GLASS_BACKGROUND[1],
            theme.GLASS_BACKGROUND[2],
        )
        fill.setAlphaF(theme.GLASS_BACKGROUND[3] / 255.0 * opacity)
        painter.setBrush(fill)
        pen = QPen(theme.qcolor(theme.GLASS_BORDER), 0.8)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        bounds = QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6)
        painter.drawRoundedRect(bounds, self._radius, self._radius)


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
    selection_requested = Signal()
    visual_region_requested = Signal()
    consent_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly PageLens")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())

        w = _pl_scaled_px(theme.PAGELENS_WIDTH)
        h = _pl_scaled_px(theme.PAGELENS_HEIGHT)
        self._default_height = h
        self._compact_height = _pl_scaled_px(197)
        self.setFixedWidth(w)
        self.resize(w, h)
        self._apply_height_limits()

        # State (initialized before any method that reads them)
        self._visible = False
        self._anchor_side = "left"
        self._bridge_connected = False
        self._surface_state = PageLensState.COMPACT

        # Three-tier readability: the window tracks hover / focus and scales
        # the glass surface alpha accordingly (idle is the most transparent).
        self._opacity_tier = "idle"
        self._exploration_expanded = False
        # Body scroll position held while the question/answer view is active,
        # restored when the back arrow returns to the concept card.
        self._saved_scroll_pos = 0

        # Image attachment state (Stage 1A)
        self._image_path: str | None = None
        self._ocr_text: str = ""
        self._ocr_status: str = "未识别"
        self._ocr_generation: int = 0
        self._ocr_thread: QThread | None = None
        self._ocr_worker: ImageOcrWorker | None = None
        self._ocr_relay = ImageOcrSignalRelay(self)
        self._ocr_relay.finished.connect(
            self._on_ocr_finished, Qt.QueuedConnection,
        )
        self._ocr_relay.error.connect(
            self._on_ocr_error, Qt.QueuedConnection,
        )

        # Image vision understanding state (Stage 1C): parallel to OCR, runs
        # the shared vision provider so the panel understands figures/formulas
        # that RapidOCR cannot. Empty string = no vision context available.
        self._vision_text: str = ""
        self._vision_thread: QThread | None = None
        self._vision_worker: ImageVisionWorker | None = None
        self._vision_relay = ImageVisionSignalRelay(self)
        self._vision_relay.finished.connect(
            self._on_vision_finished, Qt.QueuedConnection,
        )
        self._vision_relay.error.connect(
            self._on_vision_error, Qt.QueuedConnection,
        )

        # Stage 1B: store original question for UI display (outbound is augmented)
        self._last_original_question: str = ""

        # Root layout with shadow
        root = QVBoxLayout(self)
        shadow = theme.SHADOW_MARGIN - 2
        root.setContentsMargins(shadow, shadow, shadow, shadow)
        root.setSpacing(0)

        self._glass = _PageLensGlass(theme.RADIUS_CARD, self)
        self._glass.set_tier_opacity(theme.PAGELENS_OPACITY_IDLE)
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
        # Image/OCR attachment remains implemented, but is no longer a
        # permanent top-level strip in the quiet PageLens surface.
        self._image_toolbar.setVisible(False)
        inner.addWidget(self._image_toolbar)

        # Standalone collapsible OCR text panel (between toolbar and concepts)
        self._ocr_text_panel = QFrame(self._glass)
        self._ocr_text_panel.setVisible(False)
        self._ocr_text_panel.setMaximumHeight(_pl_scaled_px(220))
        self._ocr_text_panel.setStyleSheet(self._ocr_panel_style())
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

        # Separator kept for spacing parity (height math unchanged); the line
        # itself is dropped — the concepts bar's own border is enough.
        sep = QFrame(self._glass)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: transparent;")
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

        # Footer: pinned below the scroll area so the exploration entry stays
        # visible no matter how long the body content is. Must be created
        # AFTER self._content because the button expands the body questions.
        self._footer = self._build_footer()
        self._footer.setVisible(False)
        inner.addWidget(self._footer)

        # No explanation card occupies space until a real concept/selection
        # asks for one. PageLens is the single selection/region result UI.
        self._scroll.setVisible(False)

        log.info("[PageLensPanel] created")

    # ------------------------------------------------------------------
    # Footer (pinned "继续探索" entry)
    # ------------------------------------------------------------------

    def _build_footer(self) -> QFrame:
        footer = QFrame(self._glass)
        footer.setObjectName("pageLensFooter")
        footer.setStyleSheet(self._footer_style())
        footer.setFixedHeight(_pl_scaled_px(46))
        footer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(footer)
        layout.setContentsMargins(
            _pl_scaled_px(14), _pl_scaled_px(6),
            _pl_scaled_px(14), _pl_scaled_px(6),
        )

        # Secondary entry: a compact centered pill, not a full-width call to
        # action — the footer must not outweigh the reading content.
        self._explore_btn = QPushButton("继续探索", footer)
        self._explore_btn.setObjectName("pageLensExploreButton")
        self._explore_btn.setCursor(Qt.PointingHandCursor)
        self._explore_btn.setToolTip("展开本页相关的探索问题")
        self._explore_btn.setFixedHeight(_pl_scaled_px(30))
        self._explore_btn.setFixedWidth(_pl_scaled_px(150))
        self._explore_btn.clicked.connect(self._on_explore_clicked)
        self._update_explore_button_style()
        layout.addStretch(1)
        layout.addWidget(self._explore_btn)

        self._consent_btn = QPushButton("授权并解释", footer)
        self._consent_btn.setObjectName("pageLensConsentButton")
        self._consent_btn.setCursor(Qt.PointingHandCursor)
        self._consent_btn.setFixedHeight(_pl_scaled_px(30))
        self._consent_btn.setFixedWidth(_pl_scaled_px(150))
        self._consent_btn.setStyleSheet(theme.popover_button_style("pageLensConsentButton"))
        self._consent_btn.clicked.connect(self.consent_requested.emit)
        self._consent_btn.setVisible(False)
        layout.addWidget(self._consent_btn)
        layout.addStretch(1)

        return footer

    def _update_explore_button_style(self) -> None:
        accent = theme.css_color(theme.CYAN_ACCENT)
        accent_text = theme.css_color(theme.PAGELENS_TEXT_ACCENT)
        border = theme.css_color(theme.GLASS_BORDER)
        secondary = theme.css_color(theme.TEXT_SECONDARY)
        fill = self.tiered_css(theme.GLASS_BACKGROUND_SELECTED)
        base = (
            f"QPushButton#pageLensExploreButton {{ "
            f"color: {secondary}; "
            f"background: {fill}; "
            f"border: 1px solid {border}; "
            f"border-radius: {_pl_scaled_px(15)}px; "
            f"padding: 0 {_pl_scaled_px(10)}px; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(9)}pt; "
            f"font-weight: {theme.FONT_WEIGHT_MEDIUM}; "
            "}"
        )
        if self._exploration_expanded:
            # Expanded state keeps the cyan emphasis as a persistent highlight.
            self._explore_btn.setStyleSheet(
                base
                + f"QPushButton#pageLensExploreButton {{ "
                f"border: 1px solid {accent}; "
                f"color: {accent_text}; "
                "}"
            )
        else:
            self._explore_btn.setStyleSheet(
                base
                + f"QPushButton#pageLensExploreButton:hover {{ "
                f"border: 1px solid {accent}; "
                f"color: {accent_text}; "
                "}"
                f"QPushButton#pageLensExploreButton:disabled {{ "
                f"color: {theme.css_color(theme.UNAVAILABLE_STATUS)}; "
                "}"
            )

    def _on_explore_clicked(self) -> None:
        """Toggle the exploration question list inside the scrollable body."""
        self._exploration_expanded = not self._exploration_expanded
        self._content.set_questions_expanded(self._exploration_expanded)
        self._update_explore_button_style()
        self._request_height_fit()
        if self._exploration_expanded:
            QTimer.singleShot(0, self._scroll_to_questions)

    def _scroll_to_questions(self) -> None:
        sb = self._scroll.verticalScrollBar()
        qh = self._content._questions_header
        target = qh.mapTo(self._content, qh.rect().topLeft()).y() - _pl_scaled_px(4)
        sb.setValue(max(0, min(target, sb.maximum())))

    # ------------------------------------------------------------------
    # Readability tiers (idle / hover / focused)
    # ------------------------------------------------------------------

    def _apply_opacity_tier(self, tier: str) -> None:
        if tier == "focused":
            opacity = theme.PAGELENS_OPACITY_FOCUSED
        elif tier == "hover":
            opacity = theme.PAGELENS_OPACITY_HOVER
        else:
            tier = "idle"
            opacity = theme.PAGELENS_OPACITY_IDLE
        if tier == self._opacity_tier:
            return
        self._opacity_tier = tier
        self._glass.set_tier_opacity(opacity)
        # Every internal glass surface follows the tier, not just the panel
        # background — fixed-alpha children would float as bright patches on
        # the translucent surface.
        self._apply_tier_visuals()

    # -- tier-aware surface styles -----------------------------------------

    def tier_opacity(self) -> float:
        return {
            "idle": theme.PAGELENS_OPACITY_IDLE,
            "hover": theme.PAGELENS_OPACITY_HOVER,
            "focused": theme.PAGELENS_OPACITY_FOCUSED,
        }.get(self._opacity_tier, theme.PAGELENS_OPACITY_IDLE)

    def tiered_css(self, rgba: tuple[int, int, int, int]) -> str:
        return theme.css_color(theme.scale_alpha(rgba, self.tier_opacity()))

    def _concepts_bar_style(self) -> str:
        return (
            f"QFrame#pageLensTopConcepts {{ "
            f"background-color: {self.tiered_css(theme.GLASS_BACKGROUND_SELECTED)}; "
            f"border-top: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            "}"
        )

    def _footer_style(self) -> str:
        return (
            f"QFrame#pageLensFooter {{ "
            f"background-color: {self.tiered_css(theme.GLASS_BACKGROUND_SELECTED)}; "
            f"border-bottom-left-radius: {_pl_scaled_px(theme.RADIUS_CARD - 2)}px; "
            f"border-bottom-right-radius: {_pl_scaled_px(theme.RADIUS_CARD - 2)}px; "
            "}"
        )

    def _ocr_panel_style(self) -> str:
        return (
            f"background: {self.tiered_css(theme.GLASS_BACKGROUND_HOVER)}; "
            f"border: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-radius: {_pl_scaled_px(6)}px;"
        )

    def _apply_tier_visuals(self) -> None:
        """Re-style stylesheet surfaces and repaint painter children."""
        self._top_concepts.setStyleSheet(self._concepts_bar_style())
        self._footer.setStyleSheet(self._footer_style())
        self._ocr_text_panel.setStyleSheet(self._ocr_panel_style())
        self._update_explore_button_style()
        self._content.apply_tier_visuals(self.tier_opacity())
        for widget in (
            *self._top_concept_widgets,
            *self._content._related_widgets,
            *self._content._question_widgets,
        ):
            widget.update()

    def _recompute_opacity_tier(self) -> None:
        # Child widgets trigger parent leave/enter events when the cursor
        # crosses onto them, so hover is resolved from the global cursor
        # position instead of the last enter/leave event.
        if self.isActiveWindow():
            tier = "focused"
        elif self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            tier = "hover"
        else:
            tier = "idle"
        self._apply_opacity_tier(tier)

    def changeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        if event.type() == QEvent.ActivationChange:
            self._recompute_opacity_tier()
        super().changeEvent(event)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._recompute_opacity_tier()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._recompute_opacity_tier()
        super().leaveEvent(event)

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

        # Secondary PageLens actions reuse the existing PDF overlay handlers.
        self._select_btn = QPushButton("划词", header)
        self._select_btn.setObjectName("pageLensSelectMode")
        self._select_btn.setCursor(Qt.PointingHandCursor)
        self._select_btn.setToolTip("PDF 划词解释")
        self._select_btn.setFixedHeight(_pl_scaled_px(24))
        self._select_btn.setStyleSheet(theme.link_button_style("pageLensSelectMode"))
        self._select_btn.clicked.connect(self.selection_requested.emit)
        layout.addWidget(self._select_btn)

        self._region_btn = QPushButton("框选", header)
        self._region_btn.setObjectName("pageLensRegionMode")
        self._region_btn.setCursor(Qt.PointingHandCursor)
        self._region_btn.setToolTip("框选公式、图表或混合区域进行解释")
        self._region_btn.setFixedHeight(_pl_scaled_px(24))
        self._region_btn.setStyleSheet(theme.link_button_style("pageLensRegionMode"))
        self._region_btn.clicked.connect(self.visual_region_requested.emit)
        layout.addWidget(self._region_btn)

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

    def _on_top_concept_clicked(self, text: str) -> None:
        # Top-concept navigation is a new root: don't let a stale pending
        # related-navigation mark the arriving card as a related detail.
        self._content.cancel_pending_related()
        self.concept_requested.emit(text)

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

        # Start the vision understanding pass in parallel (same generation).
        vision_thread = QThread(self)
        vision_worker = ImageVisionWorker(image_path, generation=gen)
        vision_worker.moveToThread(vision_thread)
        vision_worker.finished.connect(
            self._vision_relay.forward_finished, Qt.QueuedConnection,
        )
        vision_worker.error.connect(
            self._vision_relay.forward_error, Qt.QueuedConnection,
        )
        vision_worker.finished.connect(vision_thread.quit)
        vision_worker.error.connect(vision_thread.quit)
        vision_thread.finished.connect(vision_thread.deleteLater)
        vision_worker.finished.connect(vision_worker.deleteLater)
        vision_worker.error.connect(vision_worker.deleteLater)
        vision_thread.started.connect(vision_worker.run)
        vision_thread.start()
        self._vision_thread = vision_thread
        self._vision_worker = vision_worker

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

    @Slot(int, str)
    def _on_vision_finished(self, gen: int, vision_text: str) -> None:
        """Handle vision completion (main thread via QueuedConnection)."""
        if gen != self._ocr_generation:
            return  # stale result from a previous image
        self._vision_worker = None
        self._vision_text = vision_text
        log.info("[PageLensPanel] vision understood image gen=%d", gen)

    @Slot(int, str)
    def _on_vision_error(self, gen: int, message: str) -> None:
        """Vision failed: fall back to OCR-only augmentation (zero regression)."""
        if gen != self._ocr_generation:
            return
        self._vision_worker = None
        log.warning("[PageLensPanel] vision error gen=%d: %s", gen, message)

    def _cancel_vision(self) -> None:
        """Stop accepting the current vision job and exit its thread."""
        self._vision_worker = None
        if self._vision_thread is not None:
            thread = self._vision_thread
            self._vision_thread = None
            try:
                if thread.isRunning():
                    thread.quit()
            except RuntimeError:
                pass

    def _stop_vision_thread(self) -> None:
        """Cooperatively stop the vision thread and wait for it to finish."""
        self._cancel_vision()
        if self._vision_thread is not None:
            thread = self._vision_thread
            self._vision_thread = None
            try:
                if thread.isRunning():
                    thread.wait(2000)
            except RuntimeError:
                pass

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
        """Remove the attached image and clear OCR / vision state."""
        # Invalidate any result already queued by the current worker.
        self._ocr_generation += 1
        self._stop_ocr_thread()
        self._image_path = None
        self._ocr_text = ""
        self._vision_text = ""
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
        self._cancel_vision()

    def _stop_ocr_thread(self) -> None:
        """Cooperatively stop the OCR/vision threads and wait for them.

        Called from closeEvent / remove / cancel to guarantee the native
        QThreads have exited before Python discards the widget.
        """
        self._cancel_ocr()
        if self._ocr_thread is not None:
            thread = self._ocr_thread
            self._ocr_thread = None
            try:
                if thread.isRunning():
                    thread.wait(5000)  # 5 s cooperative shutdown timeout
            except RuntimeError:
                pass
            self._ocr_worker = None
        self._stop_vision_thread()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Gracefully shut down background threads before closing."""
        self._stop_ocr_thread()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Top concepts area
    # ------------------------------------------------------------------

    def _build_top_concepts(self) -> QFrame:
        frame = QFrame(self._glass)
        frame.setObjectName("pageLensTopConcepts")
        frame.setStyleSheet(self._concepts_bar_style())
        frame.setFixedHeight(_pl_scaled_px(82))

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(
            _pl_scaled_px(14), _pl_scaled_px(8),
            _pl_scaled_px(10), _pl_scaled_px(8),
        )
        layout.setSpacing(_pl_scaled_px(4))

        concept_row = QHBoxLayout()
        concept_row.setContentsMargins(0, 0, 0, 0)
        concept_row.setSpacing(_pl_scaled_px(8))

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
        concept_row.addWidget(self._top_concepts_header, 0, Qt.AlignTop)

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
        concept_row.addWidget(self._top_concepts_scroll, 1)
        layout.addLayout(concept_row, 1)

        self._current_context_label = QLabel("", frame)
        self._current_context_label.setObjectName("pageLensCurrentContext")
        self._current_context_label.setStyleSheet(
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; "
            f"font-size: {_pl_font_px(8)}pt;"
        )
        self._current_context_label.setWordWrap(True)
        self._current_context_label.setVisible(False)
        layout.addWidget(self._current_context_label)

        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return frame

    def set_top_concepts(self, items: list[str]) -> None:
        """Render a new page's chips and return the detail area to compact."""
        # A concepts refresh means the reading context changed.  Keep the
        # compact header/context, but never leave the previous page's expanded
        # explanation underneath the new chips.
        self.collapse_to_compact()
        # Clear old chips
        for widget in self._top_concept_widgets:
            widget.deleteLater()
        self._top_concept_widgets.clear()
        while self._top_concepts_layout.count():
            self._top_concepts_layout.takeAt(0)

        # Show at most 8 concepts
        for text in (items or [])[:8]:
            chip = _TopConceptChip(text, self)
            chip.clicked.connect(self._on_top_concept_clicked)
            self._top_concepts_layout.addWidget(chip)
            self._top_concept_widgets.append(chip)
        self._top_concepts_content.updateGeometry()
        self._top_concepts_scroll.verticalScrollBar().setValue(0)
        self._top_concepts.updateGeometry()
        self._request_height_fit()

    def set_current_context(self, primary: str, secondary: str = "") -> None:
        """Show the current section/page without exposing implementation modes."""
        primary = (primary or "").strip()
        secondary = (secondary or "").strip()
        lines = [value for value in (primary, secondary) if value]
        self._current_context_label.setText("当前：\n" + "\n".join(lines) if lines else "")
        self._current_context_label.setVisible(bool(lines))
        self._top_concepts.setFixedHeight(_pl_scaled_px(116 if lines else 82))
        self._request_height_fit()

    def _screen_height_limit(self) -> int:
        # The shell itself is anchored to the primary screen; using a stale
        # pre-position frame here can accidentally pick a secondary monitor.
        screen = QApplication.primaryScreen()
        if screen is None:
            return self._default_height
        # P0.1 keeps expanded PageLens below half the usable screen. Longer
        # concept content scrolls internally instead of reclaiming the old
        # near-560px reading panel footprint.
        return max(_pl_scaled_px(320), int(screen.availableGeometry().height() * 0.48))

    def _apply_height_limits(self) -> None:
        maximum = self._screen_height_limit()
        self.setMinimumHeight(0)
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
        )

        if not self._image_toolbar.isHidden():
            natural_height += self._image_toolbar.sizeHint().height()

        # Include OCR panel height only when visible
        if hasattr(self, "_ocr_text_panel") and not self._ocr_text_panel.isHidden():
            natural_height += self._ocr_text_panel.sizeHint().height()

        natural_height += 1 + self._top_concepts.sizeHint().height() + 1
        if not self._scroll.isHidden():
            natural_height += self._content.sizeHint().height()
        if not self._footer.isHidden():
            natural_height += self._footer.sizeHint().height()
        target_height = min(
            max(self._compact_height, natural_height),
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
        self._surface_state = (
            PageLensState.VISUAL_EXPLAIN
            if card.get("_surface_kind") == "visual"
            else PageLensState.EXPLAIN
        )
        self._content.set_concept(card)
        # A fresh card starts with the exploration list collapsed.
        self._set_exploration_expanded(False)
        questions = bool(card.get("questions"))
        self._explore_btn.setEnabled(questions)
        self._explore_btn.setVisible(questions)
        self._consent_btn.setVisible(False)
        self._scroll.setVisible(bool(any(card.get(key) for key in (
            "term", "english", "summary", "context", "related", "questions",
        ))))
        self._footer.setVisible(questions)
        self._request_height_fit()

    def show_loading(self, term: str) -> None:
        self._surface_state = PageLensState.EXPLAIN
        self._content.show_loading(term)
        self._set_exploration_expanded(False)
        self._explore_btn.setEnabled(False)
        self._explore_btn.setVisible(False)
        self._consent_btn.setVisible(False)
        self._scroll.setVisible(True)
        self._footer.setVisible(False)
        self._request_height_fit()

    def show_error(self, message: str) -> None:
        self._surface_state = PageLensState.EXPLAIN
        self._content.show_error(message)
        self._set_exploration_expanded(False)
        self._explore_btn.setEnabled(False)
        self._explore_btn.setVisible(False)
        self._consent_btn.setVisible(False)
        self._scroll.setVisible(True)
        self._footer.setVisible(False)
        self._request_height_fit()

    def show_surface_loading(self, payload: dict) -> None:
        """Show controller loading inside PageLens, never in a second window."""
        title = str(payload.get("title") or "选中文本")
        page = payload.get("page")
        if isinstance(page, int) and page >= 1:
            title = f"{title} · 第 {page} 页"
        self._surface_state = (
            PageLensState.VISUAL_EXPLAIN
            if payload.get("kind") == "visual"
            else PageLensState.EXPLAIN
        )
        self._content.show_loading(title)
        self._content._summary_label.setText("流萤正在看看这里……")
        self._explore_btn.setVisible(False)
        self._consent_btn.setVisible(False)
        self._scroll.setVisible(True)
        self._footer.setVisible(False)
        self._request_height_fit()

    def show_consent_request(self, payload: dict) -> None:
        """Render the existing external-provider consent gate in PageLens."""
        title = str(payload.get("title") or "选中文本")
        page = payload.get("page")
        if isinstance(page, int) and page >= 1:
            title = f"{title} · 第 {page} 页"
        self.set_concept({
            "term": title,
            "summary": "首次解释需授权：将把你明确选择的文本发送到 AI 提供商。",
            "_standalone_surface": True,
        })
        self._explore_btn.setVisible(False)
        self._consent_btn.setVisible(True)
        self._footer.setVisible(True)
        self._request_height_fit()

    def show_surface_card(self, card: dict) -> None:
        """Render a selection/concept/visual result from the reused controller."""
        surface_card = dict(card)
        surface_card["_standalone_surface"] = True
        self.set_concept(surface_card)

    def _set_exploration_expanded(self, expanded: bool) -> None:
        self._exploration_expanded = expanded
        self._content.set_questions_expanded(expanded)
        self._update_explore_button_style()

    def show_panel(self) -> None:
        self._visible = True
        self._request_height_fit()
        self.show()
        self.raise_()
        log.info("[PageLensPanel] show")

    def hide_panel(self) -> None:
        self.collapse_to_compact()
        self._visible = False
        self.hide()
        log.info("[PageLensPanel] hide")

    def collapse_to_compact(self) -> None:
        """Hide all concept-detail state while preserving page chips/context."""
        if not hasattr(self, "_content"):
            return
        self._set_exploration_expanded(False)
        self._surface_state = PageLensState.COMPACT
        self._explore_btn.setEnabled(False)
        self._explore_btn.setVisible(True)
        self._consent_btn.setVisible(False)
        self._footer.setVisible(False)
        self._scroll.setVisible(False)
        self._content.reset_to_idle()
        self._saved_scroll_pos = 0
        self._request_height_fit()

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
        # UI displays original question, not the augmented outbound version
        display_question = self._last_original_question if self._last_original_question else question
        # Remember where the reader was in the concept card so the back arrow
        # can put them back exactly.
        if not self._content._in_question_view:
            self._saved_scroll_pos = self._scroll.verticalScrollBar().value()
        self._content.show_question_loading(parent_term, display_question)
        # Question view replaces the body; the footer entry is inert meanwhile.
        self._explore_btn.setEnabled(False)
        self._scroll.setVisible(True)
        self._footer.setVisible(False)
        self._request_height_fit()

    def _restore_related_origin(self) -> None:
        """Back arrow target for the related-concept view: the origin card."""
        origin = self._content.pop_related_origin()
        if origin is None:
            return
        self._set_exploration_expanded(False)
        self._explore_btn.setEnabled(bool(origin.get("questions")))
        self._footer.setVisible(bool(origin.get("questions")))
        self._request_height_fit()
        QTimer.singleShot(0, self._scroll_body_to_saved)

    def _save_body_scroll(self) -> None:
        self._saved_scroll_pos = self._scroll.verticalScrollBar().value()

    def _restore_concept_view(self) -> None:
        """Back arrow target: bring back the concept card behind the question view."""
        content = self._content
        if not content.has_stored_concept():
            return
        content.restore_concept_view()
        # The footer entry is inert during the question view; reactivate it
        # when the stored card carries exploration questions.
        self._explore_btn.setEnabled(bool(content._question_widgets))
        self._footer.setVisible(bool(content._question_widgets))
        self._request_height_fit()
        QTimer.singleShot(0, self._scroll_body_to_saved)

    def _scroll_body_to_saved(self) -> None:
        sb = self._scroll.verticalScrollBar()
        sb.setValue(max(0, min(self._saved_scroll_pos, sb.maximum())))

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
    def surface_state(self) -> PageLensState:
        return self._surface_state

    @property
    def anchor_side(self) -> str:
        return self._anchor_side

    def set_anchor_side(self, side: str) -> None:
        if side not in ("left", "right"):
            side = "left"
        if side != self._anchor_side:
            self._anchor_side = side
            log.info("[PageLensPanel] anchor side: %s", side)

    def _augment_question(self, question: str) -> str:
        """Augment outbound question with vision + OCR context when available.

        Returns the original question unchanged when no context exists. When
        only OCR exists the output is byte-identical to the pre-vision format
        (browser-extension compatible); the vision block is added in front
        only when the vision pass produced a description.
        """
        vision = self._vision_text.strip() if isinstance(self._vision_text, str) else ""
        ocr = self._ocr_text.strip() if isinstance(self._ocr_text, str) else ""
        if not vision and not ocr:
            return question
        if not vision:
            # Legacy format preserved verbatim for the OCR-only path.
            MAX_OCR = 6000
            TRUNCATE_HEAD = 3000
            TRUNCATE_TAIL = 3000
            if len(ocr) > MAX_OCR:
                ocr = (
                    ocr[:TRUNCATE_HEAD]
                    + "\n\n[... OCR context truncated ...]\n\n"
                    + ocr[-TRUNCATE_TAIL:]
                )
                log.info(
                    "[PageLensPanel] OCR text truncated to %d chars (head %d + tail %d)",
                    MAX_OCR, TRUNCATE_HEAD, TRUNCATE_TAIL,
                )
            return (
                "[Attached Image OCR Context]\n"
                + ocr
                + "\n[End Attached Image OCR Context]\n\n"
                "[User Question]\n"
                + question
            )
        blocks = ["[Attached Image Vision Context]\n" + vision]
        if ocr:
            blocks.append("[Attached Image OCR Context]\n" + ocr)
        return (
            "\n\n".join(blocks)
            + "\n[End Attached Image Context]\n\n"
            "[User Question]\n"
            + question
        )

    def _emit_question(self, text: str) -> None:
        log.info("[PageLensPanel] question: %s", text)
        # Store original question for UI display
        self._last_original_question = text
        # Augment outbound question with OCR context (if any)
        outbound = self._augment_question(text)
        self.question_requested.emit(outbound)


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
        opacity = _tier_opacity_of(self)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 11, 11)

        if self._hovered:
            painter.fillPath(path, theme.qcolor(theme.scale_alpha(theme.GLASS_BACKGROUND_HOVER, opacity)))
            painter.setPen(theme.qcolor(theme.CYAN_ACCENT))
        else:
            painter.fillPath(path, theme.qcolor(theme.scale_alpha(theme.GLASS_BACKGROUND, opacity)))
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
        self._title_card.setStyleSheet(self._title_card_style())
        title_layout = QVBoxLayout(self._title_card)
        title_layout.setContentsMargins(
            _pl_scaled_px(14), _pl_scaled_px(9),
            _pl_scaled_px(14), _pl_scaled_px(12),
        )
        title_layout.setSpacing(_pl_scaled_px(5))

        # --- Back button (hidden by default) ---
        self._back_btn = _BackButton(self._title_card)
        self._back_btn.hidden = True
        self._back_btn.clicked.connect(self._on_back_clicked)
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
            f"color: {theme.css_color(theme.PAGELENS_TEXT_ACCENT)}; "
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
        self._summary_sep = QFrame(self)
        self._summary_sep.setFixedHeight(1)
        self._summary_sep.setStyleSheet("background: transparent;")
        self._layout.addWidget(self._summary_sep)

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
        self._context_sep = QFrame(self)
        self._context_sep.setFixedHeight(1)
        self._context_sep.setStyleSheet("background: transparent;")
        self._layout.addWidget(self._context_sep)

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
        self._related_sep = QFrame(self)
        self._related_sep.setFixedHeight(1)
        self._related_sep.setStyleSheet("background: transparent;")
        self._layout.addWidget(self._related_sep)

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
        self._questions_wrapper = QWidget(self)
        self._questions_wrapper.setLayout(self._questions_layout)
        self._questions_wrapper.setVisible(False)
        self._layout.addWidget(self._questions_wrapper)

        # Exploration questions start collapsed; the pinned footer button
        # expands/collapses them via set_questions_expanded().
        self._questions_expanded = False
        # Unified body view state + local concept navigation memory. The
        # related-concept detail is delivered as a fresh set_concept() card,
        # so the origin card dict is pushed on a stack and re-rendered on back.
        self._view_state: _ViewState = _ViewState.VIEW_CONCEPT
        self._current_card: dict | None = None
        self._concept_stack: list[dict] = []
        self._related_pending = False

        self._concept_section_widgets = (
            self._summary_label,
            self._summary_sep,
            self._context_header,
            self._context_label,
            self._context_sep,
            self._related_header,
            self._related_sep,
            self._questions_header,
        )

        # --- Question view elements (hidden by default) ---
        self._question_parent_label = QLabel(self)
        self._question_parent_label.setStyleSheet(
            f"color: {theme.css_color(theme.PAGELENS_TEXT_ACCENT)}; "
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
        self._idle_label.setVisible(False)
        self._layout.addWidget(self._idle_label)

    # -- Concept card methods -------------------------------------------

    def set_concept(self, card: dict) -> None:
        """Render a bridge-delivered card and update the navigation state."""
        if self._related_pending:
            # This card is the related-concept detail: keep the origin card
            # locally so the back arrow can restore it.
            if self._current_card is not None:
                self._concept_stack.append(self._current_card)
            self._related_pending = False
        else:
            # Bridge-initiated navigation (top concept / page card) starts a
            # new local chain; previously saved origins are no longer valid.
            self._concept_stack.clear()
        self._render_concept(card)

    def _render_concept(self, card: dict) -> None:
        self._current_card = dict(card)
        self._show_concept_view()
        back_term = card.get("_back_term", "")
        if self._concept_stack:
            # Local navigation truth wins: the back arrow returns to the
            # origin card held on the stack.
            back_term = self._concept_stack[-1].get("term", "") or back_term
        self._back_btn.set_text(back_term)
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
        self._apply_concept_visibility(card)

    def _emit_question(self, text: str) -> None:
        # Forwarded to the owning panel, which stores the original text and
        # augments the outbound question with OCR context when available.
        if self._parent_panel is not None:
            self._parent_panel._emit_question(text)

    def show_loading(self, term: str) -> None:
        self._show_concept_view()
        loading_card = {"term": term, "summary": "加载中..."}
        # Related navigation must keep the origin card until the arriving
        # concept_card is rendered, otherwise Back would restore "加载中".
        if not self._related_pending:
            self._current_card = loading_card
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
        self._apply_concept_visibility(loading_card)

    def show_error(self, message: str) -> None:
        self._show_concept_view()
        self._current_card = {"term": "PageLens", "summary": message}
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
        self._apply_concept_visibility(self._current_card)

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

    def _title_card_style(self) -> str:
        opacity = _tier_opacity_of(self)
        return (
            f"QFrame#pageLensConceptTitleCard {{ "
            f"background-color: {theme.css_color(theme.scale_alpha(theme.GLASS_BACKGROUND_HOVER, opacity))}; "
            f"border: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-radius: {_pl_scaled_px(theme.RADIUS_ITEM)}px; "
            "}"
        )

    def apply_tier_visuals(self, _opacity: float) -> None:
        """Follow the panel tier: stylesheet surfaces here + painter children."""
        self._title_card.setStyleSheet(self._title_card_style())
        for widget in (*self._related_widgets, *self._question_widgets):
            widget.update()

    def _on_back_clicked(self) -> None:
        """Back arrow semantics depend on the active view.

        Question/answer view: restore the concept card stored behind it.
        Related-concept view: restore the origin card from the local stack.
        Concept view ("← term"): walk the concept chain via the bridge, the
        back_requested signal the panel already exposes for that purpose.
        """
        if self._parent_panel is None:
            return
        if (
            self._view_state is _ViewState.VIEW_CONCEPT
            and bool((self._current_card or {}).get("_standalone_surface"))
        ):
            self._parent_panel.collapse_to_compact()
        elif self._view_state is _ViewState.VIEW_QUESTION:
            self._parent_panel._restore_concept_view()
        elif self._view_state is _ViewState.VIEW_RELATED:
            self._parent_panel._restore_related_origin()
        else:
            self._parent_panel.back_requested.emit()

    def has_stored_concept(self) -> bool:
        """True when a concept card sits behind the active question view."""
        return bool(self._term_label.text())

    def restore_concept_view(self) -> None:
        """Re-show the concept card sections hidden by the question view."""
        self._show_concept_view()

    def set_questions_expanded(self, expanded: bool) -> None:
        """Show/hide the exploration question list inside the scrollable body."""
        self._questions_expanded = expanded
        self._apply_concept_visibility(self._current_card or {})

    def _show_concept_view(self) -> None:
        self._back_btn.hidden = False
        self._view_state = (
            _ViewState.VIEW_RELATED if self._concept_stack else _ViewState.VIEW_CONCEPT
        )
        self._term_label.setVisible(True)
        self._english_label.setVisible(True)
        self._apply_concept_visibility(self._current_card or {})
        # Hide question view
        self._question_parent_label.setVisible(False)
        self._question_title_label.setVisible(False)
        self._question_status_label.setVisible(False)
        self._question_answer_label.setVisible(False)
        self._idle_label.setVisible(False)

    def _apply_concept_visibility(self, card: dict) -> None:
        """Collapse every empty concept section instead of reserving space."""
        has_title = bool(card.get("term") or card.get("english"))
        has_summary = bool(card.get("summary"))
        has_context = bool(card.get("context"))
        has_related = bool(card.get("related"))
        has_questions = bool(card.get("questions"))

        self._title_card.setVisible(has_title)
        self._term_label.setVisible(bool(card.get("term")))
        self._english_label.setVisible(bool(card.get("english")))
        self._summary_label.setVisible(has_summary)
        self._summary_sep.setVisible(has_summary)
        self._context_header.setVisible(has_context)
        self._context_label.setVisible(has_context)
        self._context_sep.setVisible(has_context)
        self._related_header.setVisible(has_related)
        self._related_sep.setVisible(has_related)
        for widget in self._related_widgets:
            widget.setVisible(has_related)
        show_questions = has_questions and self._questions_expanded
        self._questions_header.setVisible(show_questions)
        self._questions_wrapper.setVisible(show_questions)
        for widget in self._question_widgets:
            widget.setVisible(show_questions)

    def _show_question_view(self) -> None:
        self._back_btn.hidden = False
        self._view_state = _ViewState.VIEW_QUESTION
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
        self._questions_wrapper.setVisible(False)

    def _emit_related(self, text: str) -> None:
        log.info("[PageLensPanel] related: %s", text)
        if self._parent_panel is None:
            return
        if self._current_card is not None and self._view_state is not _ViewState.VIEW_QUESTION:
            # The bridge will deliver the detail as a fresh set_concept();
            # mark the pending navigation so that call stacks this card.
            self._related_pending = True
            self._parent_panel._save_body_scroll()
        self._parent_panel.related_requested.emit(text)

    def pop_related_origin(self) -> dict | None:
        """Back from a related-concept detail: re-render the origin card."""
        if not self._concept_stack:
            return None
        origin = self._concept_stack.pop()
        self._render_concept(origin)
        return origin

    def cancel_pending_related(self) -> None:
        """A non-related navigation started; the arriving card is a new root."""
        self._related_pending = False

    def reset_to_idle(self) -> None:
        """Drop rendered detail/navigation state without touching page context."""
        self._current_card = None
        self._concept_stack.clear()
        self._related_pending = False
        self._questions_expanded = False
        self._view_state = _ViewState.VIEW_CONCEPT

        self._back_btn.hidden = True
        self._back_btn.set_text("")
        for label in (
            self._term_label,
            self._english_label,
            self._summary_label,
            self._context_label,
            self._question_parent_label,
            self._question_title_label,
            self._question_status_label,
            self._question_answer_label,
        ):
            label.setText("")

        for widget in self._related_widgets:
            widget.deleteLater()
        self._related_widgets.clear()
        while self._related_layout.count():
            self._related_layout.takeAt(0)

        for widget in self._question_widgets:
            widget.deleteLater()
        self._question_widgets.clear()
        while self._questions_layout.count():
            self._questions_layout.takeAt(0)

        self._title_card.setVisible(False)
        for widget in self._concept_section_widgets:
            widget.setVisible(False)
        self._questions_wrapper.setVisible(False)
        self._question_parent_label.setVisible(False)
        self._question_title_label.setVisible(False)
        self._question_status_label.setVisible(False)
        self._question_answer_label.setVisible(False)
        self._idle_label.setVisible(False)

    @property
    def _in_question_view(self) -> bool:
        """Backward-compatible read for the question-view flag."""
        return self._view_state is _ViewState.VIEW_QUESTION


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
        opacity = _tier_opacity_of(self)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 13, 13)

        if self._selected:
            painter.fillPath(path, theme.qcolor(theme.CYAN_ACCENT))
            painter.setPen(Qt.transparent)
        elif self._hovered:
            painter.fillPath(path, theme.qcolor(theme.scale_alpha(theme.GLASS_BACKGROUND_HOVER, opacity)))
            painter.setPen(theme.qcolor(theme.CYAN_ACCENT))
        else:
            painter.fillPath(path, theme.qcolor(theme.scale_alpha(theme.GLASS_BACKGROUND, opacity)))
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
            painter.fillPath(
                path,
                theme.qcolor(theme.scale_alpha(
                    theme.GLASS_BACKGROUND_HOVER, _tier_opacity_of(self)
                )),
            )

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
                self.rect(),
                theme.qcolor(theme.scale_alpha(
                    theme.GLASS_BACKGROUND_HOVER, _tier_opacity_of(self)
                )),
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
