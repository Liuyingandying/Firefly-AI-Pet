"""Central visual tokens and lightweight vector marks for Phase 8A.1."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QWidget


# Typography
FONT_FAMILY = "Segoe UI"
FONT_SIZE_BODY = 10
FONT_SIZE_SMALL = 8
FONT_WEIGHT_MEDIUM = 600

# Light Glass palette
TRANSPARENT = "transparent"
GLASS_BACKGROUND = (244, 250, 255, 198)
GLASS_BACKGROUND_HOVER = (249, 253, 255, 218)
GLASS_BACKGROUND_SELECTED = (218, 247, 252, 68)
TOOLBAR_ACTIVE_BACKGROUND = (211, 246, 251, 142)
GLASS_BORDER = (190, 219, 237, 92)
GLASS_BORDER_SELECTED = (125, 225, 235, 48)
SEPARATOR_COLOR = (164, 202, 225, 58)
TEXT_PRIMARY = (34, 51, 69, 255)
TEXT_SECONDARY = (86, 109, 132, 255)
CYAN_ACCENT = (83, 220, 233, 255)
MINT_STATUS = (46, 205, 187, 255)
IDLE_STATUS = (135, 179, 184, 255)
WORKING_STATUS = (79, 174, 222, 255)
WAITING_STATUS = (211, 164, 92, 255)
ERROR_STATUS = (235, 119, 125, 255)
UNAVAILABLE_STATUS = (151, 170, 188, 210)
CLAUDE_ORANGE = (232, 122, 66, 255)
CODEX_SLATE = (86, 104, 122, 255)
CHATGPT_GREEN = (39, 157, 130, 255)
SHADOW_COLOR = (64, 100, 137, 28)
PET_GLOW_INNER = (91, 232, 240, 54)
PET_GLOW_OUTER = (91, 232, 240, 0)

STATUS_COLORS = {
    "idle": IDLE_STATUS,
    "thinking": CYAN_ACCENT,
    "working": WORKING_STATUS,
    "waiting": WAITING_STATUS,
    "success": MINT_STATUS,
    "error": ERROR_STATUS,
    "sleeping": TEXT_SECONDARY,
    "unavailable": UNAVAILABLE_STATUS,
}

# Geometry
RADIUS_PILL = 36
RADIUS_TOOLBAR = 34
RADIUS_CARD = 28
RADIUS_ITEM = 22
SHADOW_BLUR = 38
SHADOW_OFFSET_Y = 8
SHADOW_MARGIN = 18
SPACE_XXS = 4
SPACE_XS = 7
SPACE_SM = 10
SPACE_MD = 14
SPACE_LG = 20
SCREEN_MARGIN = 24
CLUSTER_RIGHT_INSET = 44
CLUSTER_BOTTOM_INSET = 48
DOCK_ANCHOR_GAP = -8
TOOLBAR_ANCHOR_GAP = 2
BUBBLE_PET_OVERLAP = 62
BUBBLE_VERTICAL_OVERLAP = 68

DOCK_SIZE = QSize(462, 82)
TOOLBAR_SIZE = QSize(92, 246)
BUBBLE_SIZE = QSize(352, 138)
PET_MAX_DIMENSION = 274
PET_HORIZONTAL_PADDING = 36
PET_BOTTOM_PADDING = 34
PET_GLOW_WIDTH = 224
PET_GLOW_HEIGHT = 28

# Popover
POPOVER_WIDTH = 340
POPOVER_TEXT_WIDTH = 208
POPOVER_ANCHOR_GAP = 14


def qcolor(value: tuple[int, int, int, int]) -> QColor:
    return QColor(*value)


def css_color(value: tuple[int, int, int, int]) -> str:
    red, green, blue, alpha = value
    return f"rgba({red}, {green}, {blue}, {alpha})"


def transparent_window_style() -> str:
    return f"background: {TRANSPARENT};"


def glass_card_style(object_name: str, radius: int) -> str:
    return f"""
        QFrame#{object_name} {{
            background-color: {css_color(GLASS_BACKGROUND)};
            border: 1px solid {css_color(GLASS_BORDER)};
            border-radius: {radius}px;
        }}
    """


def primary_label_style(size: int = FONT_SIZE_BODY, weight: int = FONT_WEIGHT_MEDIUM) -> str:
    return (
        f"color: {css_color(TEXT_PRIMARY)}; background: {TRANSPARENT}; "
        f"font-family: '{FONT_FAMILY}'; font-size: {size}pt; font-weight: {weight};"
    )


def secondary_label_style(size: int = FONT_SIZE_SMALL) -> str:
    return (
        f"color: {css_color(TEXT_SECONDARY)}; background: {TRANSPARENT}; "
        f"font-family: '{FONT_FAMILY}'; font-size: {size}pt;"
    )


def interactive_surface_style(object_name: str, *, selected: bool, hovered: bool) -> str:
    if selected:
        background = GLASS_BACKGROUND_SELECTED
        border_css = TRANSPARENT
    elif hovered:
        background = GLASS_BACKGROUND_HOVER
        border_css = css_color(GLASS_BORDER)
    else:
        return f"QFrame#{object_name} {{ background: {TRANSPARENT}; border: 1px solid {TRANSPARENT}; border-radius: {RADIUS_ITEM}px; }}"
    return f"""
        QFrame#{object_name} {{
            background-color: {css_color(background)};
            border: 1px solid {border_css};
            border-radius: {RADIUS_ITEM}px;
        }}
    """


def toolbar_surface_style(object_name: str, *, selected: bool, hovered: bool) -> str:
    if selected:
        background = TOOLBAR_ACTIVE_BACKGROUND
    elif hovered:
        background = GLASS_BACKGROUND_HOVER
    else:
        background = None
    if background is None:
        return f"QFrame#{object_name} {{ background: {TRANSPARENT}; border: 1px solid {TRANSPARENT}; border-radius: {RADIUS_ITEM}px; }}"
    return f"""
        QFrame#{object_name} {{
            background-color: {css_color(background)};
            border: 1px solid {TRANSPARENT};
            border-radius: {RADIUS_ITEM}px;
        }}
    """


def separator_style(*, vertical: bool) -> str:
    side = "border-left" if vertical else "border-top"
    return f"background: {TRANSPARENT}; {side}: 1px solid {css_color(SEPARATOR_COLOR)};"


def section_label_style() -> str:
    return (
        f"color: {css_color(TEXT_SECONDARY)}; background: {TRANSPARENT}; "
        f"font-family: '{FONT_FAMILY}'; font-size: {FONT_SIZE_SMALL}pt; font-weight: {FONT_WEIGHT_MEDIUM};"
    )


def popover_button_style(object_name: str) -> str:
    return f"""
        QPushButton#{object_name} {{
            color: {css_color(TEXT_PRIMARY)};
            background: {TRANSPARENT};
            border: 1px solid {css_color(GLASS_BORDER)};
            border-radius: {RADIUS_ITEM}px;
            padding: {SPACE_XS}px {SPACE_MD}px;
            font-family: '{FONT_FAMILY}';
            font-size: {FONT_SIZE_BODY}pt;
            font-weight: {FONT_WEIGHT_MEDIUM};
        }}
        QPushButton#{object_name}:hover {{
            background: {css_color(GLASS_BACKGROUND_HOVER)};
            border: 1px solid {css_color(CYAN_ACCENT)};
        }}
    """


def link_button_style(object_name: str) -> str:
    return f"""
        QPushButton#{object_name} {{
            color: {css_color(CYAN_ACCENT)};
            background: {TRANSPARENT};
            border: none;
            padding: {SPACE_XXS}px {SPACE_XS}px;
            font-family: '{FONT_FAMILY}';
            font-size: {FONT_SIZE_SMALL}pt;
            font-weight: {FONT_WEIGHT_MEDIUM};
        }}
        QPushButton#{object_name}:hover {{
            color: {css_color(TEXT_PRIMARY)};
            background: {css_color(GLASS_BACKGROUND_HOVER)};
            border-radius: {RADIUS_ITEM}px;
        }}
    """


def bubble_html() -> str:
    primary = css_color(TEXT_PRIMARY)
    secondary = css_color(TEXT_SECONDARY)
    accent = css_color(CYAN_ACCENT)
    return (
        f"<div style=\"font-family:'{FONT_FAMILY}'; font-size:10.5pt; line-height:145%;\">"
        f"<span style=\"color:{primary}; font-weight:600;\">I'm </span>"
        f"<span style=\"color:{accent}; font-weight:600;\">Firefly!</span><br>"
        f"<span style=\"color:{secondary};\">How can I help you today?</span>"
        "</div>"
    )


def apply_soft_shadow(widget: QWidget, *, blur: int = SHADOW_BLUR, y_offset: int = SHADOW_OFFSET_Y) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y_offset)
    effect.setColor(qcolor(SHADOW_COLOR))
    widget.setGraphicsEffect(effect)


class GlassPanel(QFrame):
    """Antialiased translucent surface with a true rounded alpha silhouette."""

    def __init__(self, radius: int, parent: QWidget | None = None):
        super().__init__(parent)
        self._radius = radius
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(transparent_window_style())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(qcolor(GLASS_BACKGROUND))
        pen = QPen(qcolor(GLASS_BORDER), 0.8)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        bounds = QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6)
        painter.drawRoundedRect(bounds, self._radius, self._radius)


class StatusDot(QWidget):
    """Tiny antialiased lifecycle indicator using the shared status palette."""

    def __init__(self, state: str = "unavailable", parent: QWidget | None = None):
        super().__init__(parent)
        self._state = state
        self.setFixedSize(10, 10)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        self._state = state if state in STATUS_COLORS else "unavailable"
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(qcolor(STATUS_COLORS[self._state]))
        painter.drawEllipse(QRectF(2, 2, 6, 6))


class VectorIcon(QWidget):
    """Small dependency-free vector mark for agents and toolbar actions."""

    def __init__(
        self,
        kind: str,
        color: tuple[int, int, int, int],
        size: int = 26,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._kind = kind
        self._color = color
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_color(self, color: tuple[int, int, int, int]) -> None:
        self._color = color
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.translate(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) * 0.38
        pen = QPen(qcolor(self._color), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        if self._kind in {"star", "companion"}:
            self._draw_star(painter, radius)
        elif self._kind in {"hexagon", "workspace", "codex"}:
            self._draw_hexagon(painter, radius, inner=self._kind == "codex")
        elif self._kind in {"gear", "settings"}:
            self._draw_gear(painter, radius)
        elif self._kind == "claude":
            self._draw_claude(painter, radius)
        elif self._kind == "chatgpt":
            self._draw_chatgpt(painter, radius)
        else:
            painter.drawEllipse(QRectF(-radius, -radius, radius * 2, radius * 2))

    @staticmethod
    def _draw_star(painter: QPainter, radius: float) -> None:
        path = QPainterPath()
        path.moveTo(0, -radius)
        path.cubicTo(radius * 0.08, -radius * 0.25, radius * 0.25, -radius * 0.08, radius, 0)
        path.cubicTo(radius * 0.25, radius * 0.08, radius * 0.08, radius * 0.25, 0, radius)
        path.cubicTo(-radius * 0.08, radius * 0.25, -radius * 0.25, radius * 0.08, -radius, 0)
        path.cubicTo(-radius * 0.25, -radius * 0.08, -radius * 0.08, -radius * 0.25, 0, -radius)
        painter.setBrush(painter.pen().color())
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)

    @staticmethod
    def _draw_hexagon(painter: QPainter, radius: float, *, inner: bool) -> None:
        points = [
            QPointF(math.cos(math.radians(60 * i - 30)) * radius, math.sin(math.radians(60 * i - 30)) * radius)
            for i in range(6)
        ]
        painter.drawPolygon(QPolygonF(points))
        if inner:
            inset = radius * 0.48
            diamond = QPolygonF([QPointF(0, -inset), QPointF(inset, 0), QPointF(0, inset), QPointF(-inset, 0)])
            painter.drawPolygon(diamond)

    @staticmethod
    def _draw_gear(painter: QPainter, radius: float) -> None:
        points: list[QPointF] = []
        for index in range(24):
            angle = math.radians(index * 15 - 90)
            step = index % 3
            length = radius if step == 1 else radius * 0.78
            points.append(QPointF(math.cos(angle) * length, math.sin(angle) * length))
        painter.drawPolygon(QPolygonF(points))
        hole = radius * 0.28
        painter.drawEllipse(QRectF(-hole, -hole, hole * 2, hole * 2))

    @staticmethod
    def _draw_claude(painter: QPainter, radius: float) -> None:
        for index in range(12):
            angle = math.radians(index * 30)
            inner = radius * 0.30
            painter.drawLine(
                QPointF(math.cos(angle) * inner, math.sin(angle) * inner),
                QPointF(math.cos(angle) * radius, math.sin(angle) * radius),
            )
        painter.setBrush(painter.pen().color())
        painter.setPen(Qt.NoPen)
        core = radius * 0.22
        painter.drawEllipse(QRectF(-core, -core, core * 2, core * 2))

    @staticmethod
    def _draw_chatgpt(painter: QPainter, radius: float) -> None:
        loop_radius = radius * 0.45
        for index in range(6):
            painter.save()
            painter.rotate(index * 60)
            painter.drawArc(QRectF(-loop_radius, -radius * 0.72, loop_radius * 2, radius * 0.9), 20 * 16, 205 * 16)
            painter.restore()
