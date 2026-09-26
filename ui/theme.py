"""Central visual tokens and lightweight vector marks for Phase 8A.1."""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QFrame, QGraphicsDropShadowEffect, QWidget


# Typography
FONT_FAMILY = "Segoe UI"
FONT_SIZE_BODY = 10
FONT_SIZE_SMALL = 8
FONT_WEIGHT_MEDIUM = 600

# Light Glass palette
TRANSPARENT = "transparent"
GLASS_BACKGROUND = (244, 250, 255, 250)
GLASS_BACKGROUND_HOVER = (249, 253, 255, 252)
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
QWEN_VIOLET = (111, 92, 201, 255)
ZCODE_BLUE = (86, 141, 222, 255)
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


# -- UI V2 companion console palette (Phase UI-4A) ---------------------------
# Centralized semantic colors for the "流萤 AI Pet 控制台" surface only.
# Deliberately namespaced (theme.V2.*) so the existing pet-overlay islands
# keep their own palette untouched.
#
# Phase UI-4A direction (per reference README): Anthropic 简约工作台 + 星铁
# 菜单式布局 + 流萤陪伴空间 —— 暖纸浅色系、大留白、高字号、低饱和、少量青紫
# 点缀。禁止深色赛博 HUD / 霓虹发光 / 小字体信息墙。GLOW_* 降为低 alpha 的
# 柔和点缀色（仅细边框/极浅投影），不再承担发光。

class V2:
    """Semantic palette for the companion console (warm paper light theme)."""

    # 字体栈族名（科研级渲染，Phase UI-4 字体系统）：
    #   STIX Two Math  数学/物理公式优先（应用级资源可加载）
    #   Cambria Math   Windows 自带数学兜底
    #   雅黑/Segoe     中文与西文正文
    #   Noto CJK/DejaVu/Symbola  CJK、扩展符号、技术字母兜底
    FONT_FAMILY_PRIMARY = "Microsoft YaHei UI"
    FONT_FAMILY_MATH = "Cambria Math"
    FONT_FAMILY_FALLBACK = "Noto Sans CJK SC"
    FONT_STACK_FAMILIES = (
        "STIX Two Math",
        "Cambria Math",
        "Microsoft YaHei UI",
        "Segoe UI",
        "Noto Sans CJK SC",
        "DejaVu Sans",
        "Symbola",
    )

    # 背景与卡片：暖米纸面 + 暖白卡片
    BACKGROUND = (242, 238, 230, 255)          # 暖米纸面
    BACKGROUND_DEEP = (234, 228, 216, 255)     # 略深暖米（渐变边缘）
    CARD_BG = (251, 249, 244, 245)             # 暖白卡片
    CARD_BG_USER = (228, 238, 243, 240)        # 用户卡片 · 浅青蓝
    CARD_BG_ASSISTANT = (241, 238, 247, 240)   # 流萤卡片 · 淡紫白

    # 品牌点缀：低饱和青 / 紫
    PRIMARY_BLUE = (90, 155, 181, 255)
    ACCENT_PURPLE = (139, 127, 199, 255)

    # 文字：Anthropic 深棕灰系（浅底高可读）
    TEXT_MAIN = (61, 57, 41, 255)
    TEXT_SECONDARY = (138, 133, 120, 255)

    # 柔光：低 alpha 点缀（细边框/极浅投影），替代霓虹发光
    GLOW_BLUE = (90, 155, 181, 40)
    GLOW_PURPLE = (139, 127, 199, 45)
    BORDER_SOFT = (224, 217, 204, 180)

    # 背景渐变：暖米纸面（几乎单色，留白感）
    GRADIENT_STOPS = ("#f6f2ea", "#f1ece2", "#ece5d8")

    # 字体等级（Phase UI-4A 定义；组件消费在 Phase UI-4B）。
    # "高字号"要求：说明不低于 9pt，正文 11pt，标题 16pt。
    FONT_TITLE = 16
    FONT_HEADING = 13
    FONT_BODY = 11
    FONT_CAPTION = 9


# QSS font-family fallback stack for every V2 surface（科研级渲染，数学优先）：
# STIX Two Math（公式）→ Cambria Math → 雅黑/Segoe（中西文）→ Noto CJK →
# DejaVu Sans（技术符号）→ Symbola（罕用符号兜底）。
V2_FONT_STACK = ", ".join(f'"{family}"' for family in V2.FONT_STACK_FAMILIES)


def v2_font_css(size_pt: int) -> str:
    """QSS 片段：字号 + 字体栈（测试与组件统一入口）。"""
    return f"font-family: {V2_FONT_STACK}; font-size: {size_pt}pt;"


# 应用级字体资源目录（assets/fonts/，Phase UI-4 字体系统）。
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_LOADED_APP_FONTS: list[str] = []
_FONTS_LOADED = False


def load_application_fonts(directory: Path | None = None) -> list[str]:
    """Register every font file under ``assets/fonts/`` with QFontDatabase.

    Idempotent (second call is a no-op) and fully optional: a missing or
    empty directory silently skips loading — the font stack above still
    resolves through system-installed families. Returns the family names
    contributed by application fonts (for diagnostics/tests).

    Intended to be called once after QApplication creation (theme-side, no
    core dependencies).
    """
    global _FONTS_LOADED, _LOADED_APP_FONTS
    if _FONTS_LOADED:
        return list(_LOADED_APP_FONTS)
    fonts_dir = Path(directory) if directory is not None else FONTS_DIR
    families: list[str] = []
    for pattern in ("*.ttf", "*.otf"):
        for font_file in sorted(fonts_dir.glob(pattern)):
            try:
                font_id = QFontDatabase.addApplicationFont(str(font_file))
            except Exception:  # malformed font file must never break startup
                continue
            if font_id == -1:
                continue
            for family in QFontDatabase.applicationFontFamilies(font_id):
                if family not in families:
                    families.append(family)
    _LOADED_APP_FONTS = families
    _FONTS_LOADED = True
    return list(_LOADED_APP_FONTS)


_BACKGROUND_ASSET = (
    Path(__file__).resolve().parent.parent / "assets" / "ui" / "background" / "background.png"
)


def v2_background_style(asset_path: Path | None = None) -> str:
    """Console background QSS: assets/ui/background image if present,
    otherwise the code-drawn deep-space gradient (no absolute paths).

    ``asset_path`` overrides the documented asset location (test seam).
    """
    asset = asset_path if asset_path is not None else _BACKGROUND_ASSET
    if asset.is_file():
        # Qt stylesheets accept forward-slash drive paths on Windows.
        return "QMainWindow { border-image: url(" + asset.as_posix() + ") 0 0 0 0 stretch stretch; }"
    stops = V2.GRADIENT_STOPS
    return (
        "QMainWindow {"
        "  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
        f"    stop:0 {stops[0]}, stop:0.55 {stops[1]}, stop:1 {stops[2]});"
        "}"
    )

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

# AgentDock — slim launcher pill tokens (Phase 8B compact).
# The window keeps just enough shadow margin for the lighter drop shadow;
# the visible pill (card) is ~36-42px tall, ~290-300px wide at 100% DPI.
DOCK_SIZE = QSize(320, 58)
COMPACT_DOCK_SIZE = QSize(220, 58)
DOCK_CARD_RADIUS = 20
DOCK_CARD_MARGIN = 4
DOCK_ITEM_MARGIN = 2
DOCK_ITEM_SPACING = 2
DOCK_DIVIDER_HEIGHT = 20
DOCK_SHADOW_BLUR = 22
DOCK_SHADOW_OFFSET_Y = 5
DOCK_SHADOW_MARGIN = 12
DOCK_SHADOW_COLOR = (64, 100, 137, 16)
DOCK_LETTER_FONT_PT = 9
DOCK_NAME_FONT_PT = 6
DOCK_STATUS_DOT_SIZE = 7
DOCK_STATUS_DOT_GAP = 6
DOCK_STATUS_GREEN = (64, 196, 138, 255)
DOCK_STATUS_RED = ERROR_STATUS
DOCK_STATUS_GRAY = (162, 178, 193, 215)
FULL_DOCK_SCALE_THRESHOLD = 0.90
# Four primary actions only.  The old 10-action toolbar was 583 px tall;
# keeping this as an explicit shell token makes hidden actions unable to leave
# empty layout slots behind.
TOOLBAR_SIZE = QSize(92, 340)  # 5 items (companion/scratchpad/pagelens/workspace/settings)
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

# PageLens Panel (Phase 9A.1 — persistent reading panel)
# Reading dimensions are FIXED logical pixels, independent of Pet scale.
# They only respond to true system UI/DPI scale, not the Pet character scale.
PAGELENS_WIDTH = 460
PAGELENS_HEIGHT = 560
PAGELENS_MIN_WIDTH = 400
PAGELENS_MIN_HEIGHT = 420
PAGELENS_ANCHOR_GAP = 12
PAGELENS_HEADER_HEIGHT = 48
# PageLens readability tiers: glass surface fill opacity (idle / hover / focused).
# The surface becomes more transparent when idle to reduce occlusion while
# reading the page behind, and clears up on hover / window focus.
PAGELENS_OPACITY_IDLE = 0.70
PAGELENS_OPACITY_HOVER = 0.82
PAGELENS_OPACITY_FOCUSED = 0.92
# Readable accent for PageLens text on light glass (English subtitle, footer
# entry). CYAN_ACCENT stays reserved for borders / status labels / highlights —
# it is far too light for body-adjacent text (contrast ~1.6:1 on glass).
PAGELENS_TEXT_ACCENT = (23, 128, 140, 255)


def scale_alpha(
    rgba: tuple[int, int, int, int], opacity: float
) -> tuple[int, int, int, int]:
    """Scale an RGBA token's alpha by a tier opacity (clamped to 0-255)."""
    red, green, blue, alpha = rgba
    scaled = int(round(alpha * opacity))
    return (red, green, blue, max(0, min(255, scaled)))


# Runtime UI scale (Firefly's own logical scaling on top of Qt DPI). This is the
# single source of truth for the whole visual shell: pet, dock, toolbar, bubble,
# offsets, icons and fonts all derive from it via the helpers below.
MIN_SCALE = 0.60
DEFAULT_SCALE = 1.00
MAX_SCALE = 1.40
SCALE_STEP = 0.05
MIN_FONT_PX = 7

_ui_scale = DEFAULT_SCALE
_scale_listeners: list = []


def ui_scale() -> float:
    return _ui_scale


def set_ui_scale(value: float) -> float:
    """Clamp and apply a new scale; notifies listeners only when it changed."""
    global _ui_scale
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = DEFAULT_SCALE
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        value = DEFAULT_SCALE
    value = min(MAX_SCALE, max(MIN_SCALE, value))
    value = round(value, 2)
    if value != _ui_scale:
        _ui_scale = value
        for listener in list(_scale_listeners):
            listener(_ui_scale)
    return _ui_scale


def on_scale_changed(listener) -> None:
    _scale_listeners.append(listener)


def scaled(value: int | float) -> int:
    """Scale an offset/gap (may be negative); no minimum clamp."""
    return int(round(value * _ui_scale))


def scaled_px(value: int | float) -> int:
    """Scale a pixel size; never below 1px."""
    return max(1, scaled(value))


def scaled_font_px(value: int | float) -> int:
    """Scale a font size; never below the readability floor."""
    return max(MIN_FONT_PX, scaled(value))


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
        f"font-family: '{FONT_FAMILY}'; font-size: {scaled_font_px(size)}pt; font-weight: {weight};"
    )


def secondary_label_style(size: int = FONT_SIZE_SMALL) -> str:
    return (
        f"color: {css_color(TEXT_SECONDARY)}; background: {TRANSPARENT}; "
        f"font-family: '{FONT_FAMILY}'; font-size: {scaled_font_px(size)}pt;"
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
        f"font-family: '{FONT_FAMILY}'; font-size: {scaled_font_px(FONT_SIZE_SMALL)}pt; font-weight: {FONT_WEIGHT_MEDIUM};"
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
            font-size: {scaled_font_px(FONT_SIZE_BODY)}pt;
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
            font-size: {scaled_font_px(FONT_SIZE_SMALL)}pt;
            font-weight: {FONT_WEIGHT_MEDIUM};
        }}
        QPushButton#{object_name}:hover {{
            color: {css_color(TEXT_PRIMARY)};
            background: {css_color(GLASS_BACKGROUND_HOVER)};
            border-radius: {RADIUS_ITEM}px;
        }}
    """


def bubble_html(display_names=None) -> str:
    from character import CharacterDisplayNames

    names = display_names or CharacterDisplayNames()
    primary = css_color(TEXT_PRIMARY)
    secondary = css_color(TEXT_SECONDARY)
    accent = css_color(CYAN_ACCENT)
    return (
        f"<div style=\"font-family:'{FONT_FAMILY}'; font-size:{scaled_font_px(10.5)}pt; line-height:145%;\">"
        f"<span style=\"color:{primary}; font-weight:600;\">I'm </span>"
        f"<span style=\"color:{accent}; font-weight:600;\">{names.display_name}!</span><br>"
        f"<span style=\"color:{secondary};\">How can I help you today?</span>"
        "</div>"
    )


def apply_soft_shadow(
    widget: QWidget,
    *,
    blur: int = SHADOW_BLUR,
    y_offset: int = SHADOW_OFFSET_Y,
    color: tuple[int, int, int, int] = SHADOW_COLOR,
) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y_offset)
    effect.setColor(qcolor(color))
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
        elif self._kind in {"claude"}:
            self._draw_claude(painter, radius)
        elif self._kind in {"chatgpt"}:
            self._draw_chatgpt(painter, radius)
        elif self._kind in {"pagelens"}:
            self._draw_pagelens(painter, radius)
        elif self._kind in {"paper"}:
            self._draw_paper(painter, radius)
        elif self._kind in {"memory"}:
            self._draw_memory(painter, radius)
        elif self._kind in {"console"}:
            self._draw_console(painter, radius)
        elif self._kind in {"note", "scratchpad"}:
            self._draw_note(painter, radius)
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

    @staticmethod
    def _draw_pagelens(painter: QPainter, radius: float) -> None:
        """A 2x2 grid (four small rectangles) suggesting pages / a lens."""
        painter.setPen(painter.pen().color())
        painter.setBrush(Qt.NoBrush)
        half = radius * 0.55
        gap = radius * 0.08
        w = half - gap
        # top-left
        painter.drawRect(QRectF(-half, -half, w, w))
        # top-right
        painter.drawRect(QRectF(gap, -half, w, w))
        # bottom-left
        painter.drawRect(QRectF(-half, gap, w, w))
        # bottom-right
        painter.drawRect(QRectF(gap, gap, w, w))

    @staticmethod
    def _draw_paper(painter: QPainter, radius: float) -> None:
        """A document glyph: page outline with a folded corner and two lines."""
        painter.setPen(painter.pen().color())
        painter.setBrush(Qt.NoBrush)
        half_w = radius * 0.85
        half_h = radius * 1.05
        corner = radius * 0.32
        path = QPainterPath()
        path.moveTo(-half_w, -half_h)
        path.lineTo(half_w - corner, -half_h)
        path.lineTo(half_w, -half_h + corner)
        path.lineTo(half_w, half_h)
        path.lineTo(-half_w, half_h)
        path.closeSubpath()
        painter.drawPath(path)
        # Fold triangle
        painter.drawLine(half_w - corner, -half_h, half_w - corner, -half_h + corner)
        painter.drawLine(half_w - corner, -half_h + corner, half_w, -half_h + corner)
        # Text lines
        painter.drawLine(-half_w + radius * 0.28, -radius * 0.18, half_w - radius * 0.28, -radius * 0.18)
        painter.drawLine(-half_w + radius * 0.28, radius * 0.12, half_w - radius * 0.28, radius * 0.12)

    @staticmethod
    @staticmethod
    def _draw_note(painter: QPainter, radius: float) -> None:
        """A small notepad: rounded page with a folded corner and two lines."""
        size = radius * 1.7
        left, top = -size / 2, -size / 2
        fold = size * 0.30
        path = QPainterPath()
        path.moveTo(left, top)
        path.lineTo(left + size - fold, top)
        path.lineTo(left + size, top + fold)
        path.lineTo(left + size, top + size)
        path.lineTo(left, top + size)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(left + size - fold, top),
                         QPointF(left + size - fold, top + fold))
        painter.drawLine(QPointF(left + size - fold, top + fold),
                         QPointF(left + size, top + fold))
        line_y = top + size * 0.55
        painter.drawLine(QPointF(left + size * 0.2, line_y),
                         QPointF(left + size * 0.8, line_y))
        painter.drawLine(QPointF(left + size * 0.2, line_y + size * 0.22),
                         QPointF(left + size * 0.62, line_y + size * 0.22))

    def _draw_memory(painter: QPainter, radius: float) -> None:
        """A simple cylinder / database shape suggesting stored data."""
        painter.setPen(painter.pen().color())
        painter.setBrush(Qt.NoBrush)
        # Top ellipse
        painter.drawEllipse(QRectF(-radius * 0.7, -radius, radius * 1.4, radius * 0.5))
        # Body lines
        painter.drawLine(-radius * 0.7, -radius * 0.75, -radius * 0.7, radius * 0.5)
        painter.drawLine(radius * 0.7, -radius * 0.75, radius * 0.7, radius * 0.5)
        # Bottom ellipse
        painter.drawEllipse(QRectF(-radius * 0.7, radius * 0.3, radius * 1.4, radius * 0.5))

    @staticmethod
    def _draw_console(painter: QPainter, radius: float) -> None:
        """A terminal window: rounded frame, prompt line and cursor line."""
        painter.setPen(painter.pen().color())
        painter.setBrush(Qt.NoBrush)
        # Window frame
        painter.drawRoundedRect(
            QRectF(-radius * 0.85, -radius * 0.7, radius * 1.7, radius * 1.4),
            radius * 0.25, radius * 0.25,
        )
        # Prompt line
        painter.drawLine(-radius * 0.55, -radius * 0.25, radius * 0.35, -radius * 0.25)
        # Cursor line
        painter.drawLine(-radius * 0.55, radius * 0.25, radius * 0.15, radius * 0.25)


# -- PageLens mock data (Phase 9A static shell) ------------------------------

PAGELENS_MOCK_CONCEPT = {
    "term": "相位裕度",
    "english": "Phase Margin",
    "summary": (
        "衡量闭环系统距离不稳定状态还有多少相位余量。"
        "相位裕度越大，系统越稳定，但响应可能变慢；"
        "相位裕度太小，系统可能出现振荡甚至发散。"
    ),
    "context": (
        "当前测试内容用于验证 PageLens 桌面面板的布局、"
        "尺寸、滚动与跟随行为。"
    ),
    "related": ["GBW", "Cf", "噪声增益"],
    "questions": [
        "Cf 如何影响相位裕度？",
        "GBW 为什么影响稳定性？",
    ],
}
