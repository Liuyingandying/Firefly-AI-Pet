"""Motion — 「流萤」主题的轻动效组件库 (ui美化版).

围绕「流萤 = 发光、轻盈」建立的统一微动效语言, 全部为纯 QWidget/QPainter
实现, 不引入第三方依赖:

- ``BreathingDot``   慢呼吸状态点（在线指示, ~2.4s 呼吸周期）
- ``ThinkingDots``   三个跳动的紫色光点（AI 思考中）
- ``StatusLamp``     状态指示灯（思考=紫呼吸 / 成功=绿常亮 / 出错=红闪烁）
- ``ToggleSwitch``   胶囊开关（深度思考, 开启时紫色）
- ``fade_rise_in``   消息入场: 淡入 + 轻微上浮
- ``press_bounce``   按钮 0.95 倍缩放回弹
- ``attach_hover_glow`` hover 时萤火微光（紫外发光）

所有动画组件自管理 QTimer/QVariantAnimation, 父对象销毁时自动停止。
"""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPropertyAnimation,
    QEasingCurve,
    QTimer,
    QVariantAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QWidget,
)

from ui import theme


# --------------------------------------------------------------------------- #
# 呼吸状态点
# --------------------------------------------------------------------------- #

class BreathingDot(QWidget):
    """慢呼吸圆点: 亮度与半径随正弦缓慢起伏, 用于「在线」状态感知."""

    def __init__(
        self,
        color: tuple[int, int, int, int] = theme.V2.PRIMARY,
        diameter: int = 10,
        period_ms: int = 2400,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(diameter + 8, diameter + 8)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._period = max(400, period_ms)
        self._elapsed = 0
        self._timer.start(40)

    def set_color(self, color: tuple[int, int, int, int]) -> None:
        self._color = color
        self.update()

    def _tick(self) -> None:
        self._elapsed += 40
        self._phase = (self._elapsed % self._period) / self._period
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        wave = 0.5 + 0.5 * math.sin(self._phase * 2 * math.pi)
        side = min(self.width(), self.height())
        base = side - 8
        dot = base * (0.72 + 0.28 * wave)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        center = self.rect().center()
        # 外圈微光（呼吸 halo）
        halo = QRadialGradient(center, base * 0.9)
        color = QColor(*self._color)
        halo_color = QColor(color)
        halo_color.setAlpha(int(30 + 70 * wave))
        halo.setColorAt(0.0, halo_color)
        transparent = QColor(color)
        transparent.setAlpha(0)
        halo.setColorAt(1.0, transparent)
        painter.setBrush(halo)
        painter.drawEllipse(center, int(base * 0.9), int(base * 0.9))
        # 实心点
        core = QColor(color)
        core.setAlpha(int(150 + 105 * wave))
        painter.setBrush(core)
        painter.drawEllipse(
            center, int(dot / 2), int(dot / 2)
        )


# --------------------------------------------------------------------------- #
# AI 思考跳动光点
# --------------------------------------------------------------------------- #

class ThinkingDots(QWidget):
    """三个错相跳动的紫色光点（替代纯文字的「AI 正在思考…」提示）."""

    def __init__(
        self,
        color: tuple[int, int, int, int] = theme.V2.PRIMARY,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(56, 20)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def _tick(self) -> None:
        self._elapsed += 50
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        color = QColor(*self._color)
        center_y = self.height() / 2
        for index in range(3):
            phase = (self._elapsed / 900.0) - index * 0.22
            wave = 0.5 + 0.5 * math.sin(phase * 2 * math.pi)
            x = 12 + index * 16
            y = center_y + 4 - 7 * wave
            radius = 2.6 + 1.2 * wave
            dot = QColor(color)
            dot.setAlpha(int(120 + 135 * wave))
            painter.setBrush(dot)
            diameter = int(radius * 2)
            painter.drawEllipse(int(x - radius), int(y - radius), diameter, diameter)


# --------------------------------------------------------------------------- #
# 状态指示灯
# --------------------------------------------------------------------------- #

class StatusLamp(QWidget):
    """右侧状态卡指示灯: thinking/working=紫呼吸, success=绿常亮,
    error=红闪烁, 其余=低亮常灰."""

    _BEHAVIOR = {
        "thinking": ("breathe", theme.V2.PRIMARY),
        "working": ("breathe", theme.V2.PRIMARY),
        "tool_running": ("breathe", theme.V2.PRIMARY),
        "waiting_input": ("breathe", theme.V2.PRIMARY),
        "success": ("steady", theme.MINT_STATUS),
        "error": ("blink", theme.ERROR_STATUS),
        "idle": ("steady", theme.V2.TEXT_SECONDARY),
    }

    def __init__(self, state: str = "idle", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)
        self.set_state(state)

    def set_state(self, state: str) -> None:
        behavior, color = self._BEHAVIOR.get(state, ("steady", theme.V2.TEXT_SECONDARY))
        self._behavior = behavior
        self._color = color
        self.update()

    def _tick(self) -> None:
        self._elapsed += 50
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._behavior == "breathe":
            wave = 0.5 + 0.5 * math.sin(self._elapsed / 1600.0 * 2 * math.pi)
        elif self._behavior == "blink":
            wave = 1.0 if (self._elapsed % 700) < 380 else 0.15
        else:
            wave = 1.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        color = QColor(*self._color)
        glow = QColor(color)
        glow.setAlpha(int(28 + 60 * wave))
        painter.setBrush(glow)
        painter.drawEllipse(1, 1, 10, 10)
        core = QColor(color)
        core.setAlpha(int(120 + 135 * wave))
        painter.setBrush(core)
        painter.drawEllipse(3, 3, 6, 6)


# --------------------------------------------------------------------------- #
# 深度思考 Toggle 开关
# --------------------------------------------------------------------------- #

class ToggleSwitch(QWidget):
    """胶囊 Toggle: 关=灰白轨道, 开=品牌紫轨道, knob 带滑动动画."""

    toggled = Signal(bool)

    def __init__(
        self,
        checked: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(38, 20)
        self.setCursor(Qt.PointingHandCursor)
        self._checked = bool(checked)
        self._pos = 1.0 if checked else 0.0  # knob 位置 0..1
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.valueChanged.connect(self._on_pos)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def _on_pos(self, value: float) -> None:
        self._pos = float(value)
        self.update()

    def isChecked(self) -> bool:  # noqa: N802
        return self._checked

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self._animate_to(1.0 if checked else 0.0)
        self.toggled.emit(checked)

    def _animate_to(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(target)
        self._anim.start()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        # 轨道
        if self._pos > 0.5:
            track = QColor(*theme.V2.PRIMARY)
        else:
            track = QColor(214, 210, 220, 255)
        painter.setBrush(track)
        painter.drawRoundedRect(0, 0, 38, 20, 10, 10)
        # knob
        knob_x = 3 + (38 - 14 - 6) * self._pos
        painter.setBrush(QColor(255, 255, 255, 255))
        painter.drawEllipse(int(knob_x), 3, 14, 14)
        # 开启时 knob 带一点微光
        if self._pos > 0.5:
            painter.setPen(QPen(QColor(155, 109, 255, 60), 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(int(knob_x), 3, 14, 14)


# --------------------------------------------------------------------------- #
# 入场 / 点击动画
# --------------------------------------------------------------------------- #

def fade_rise_in(widget: QWidget, *, offset: int = 10, duration: int = 260) -> None:
    """消息入场: 纯透明度淡入（布局安全实现）.

    v4 修复: 早期版本同时做「上浮」geometry 动画 —— 在 Qt 布局管理的
    容器里对子项改 geometry 会与布局重排打架: 连续快速插入多条消息时
    （如学习模式切换一次 append 两三条）, 布局不断重排而动画朝过期
    位置写入, 结束后 widget 停留在错误的 y 位置 → 消息卡片互相重叠。
    现在只动 QGraphicsOpacityEffect 的 opacity（不触碰 geometry）,
    从根上消除冲突; ``offset`` 参数保留仅为兼容旧签名。
    """
    del offset  # geometry 上浮已移除（布局安全优先）

    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)

    fade = QPropertyAnimation(effect, b"opacity", widget)
    fade.setDuration(duration)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QEasingCurve.OutCubic)
    # 动画结束后摘除 effect: 避免 opacity=1 的 QGraphicsOpacityEffect
    # 常驻渲染管线（也消除截图/绘制时的源 pixmap 竞争警告）。
    fade.finished.connect(lambda: widget.setGraphicsEffect(None))
    fade.start(QPropertyAnimation.DeleteWhenStopped)


def press_bounce(widget: QWidget, *, scale: float = 0.95, duration: int = 200) -> None:
    """点击反馈: 透明度快速脉冲（≈0.95 缩放的轻量替代）.

    v4 修复: 早期版本用 geometry 缩放做回弹, 与 fade_rise_in 同样存在
    布局冲突风险（按钮在布局中错位）。改为不碰 geometry 的 opacity
    脉冲 + 各按钮 QSS 的 :pressed 深色态, 视觉反馈等效且绝对安全。
    ``scale`` 参数保留仅为兼容旧签名。
    """
    del scale, duration

    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    pulse = QPropertyAnimation(effect, b"opacity", widget)
    pulse.setDuration(180)
    pulse.setStartValue(1.0)
    pulse.setKeyValueAt(0.4, 0.72)
    pulse.setEndValue(1.0)
    pulse.setEasingCurve(QEasingCurve.OutCubic)
    pulse.finished.connect(lambda: widget.setGraphicsEffect(None))
    pulse.start(QPropertyAnimation.DeleteWhenStopped)


# --------------------------------------------------------------------------- #
# 发光 helpers
# --------------------------------------------------------------------------- #

def purple_shadow(
    widget: QWidget,
    *,
    blur: int = 14,
    y_offset: int = 2,
    alpha: int | None = None,
) -> QGraphicsDropShadowEffect:
    """统一紫调软阴影: 0 2px 12px rgba(155,109,255,0.08) 的 Qt 等价实现."""
    if alpha is None:
        alpha = theme.V2.SHADOW_PURPLE[3]
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y_offset)
    effect.setColor(QColor(*theme.V2.SHADOW_PURPLE[:3], alpha))
    widget.setGraphicsEffect(effect)
    return effect


class _HoverGlowFilter(QObject):
    """enter/leave 时挂/摘紫外发光效果（萤火微光）."""

    def __init__(self, button: QWidget, glow: tuple[int, int, int, int], blur: int) -> None:
        super().__init__(button)
        self._button = button
        self._glow = glow
        self._blur = blur
        self._effect: QGraphicsDropShadowEffect | None = None

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self._button:
            if event.type() == QEvent.Enter:
                self._effect = QGraphicsDropShadowEffect(self._button)
                self._effect.setBlurRadius(self._blur)
                self._effect.setOffset(0, 0)
                self._effect.setColor(QColor(*self._glow))
                self._button.setGraphicsEffect(self._effect)
            elif event.type() == QEvent.Leave:
                self._button.setGraphicsEffect(None)
                self._effect = None
        return super().eventFilter(watched, event)


def attach_hover_glow(
    button: QWidget,
    glow: tuple[int, int, int, int] = theme.V2.PRIMARY_GLOW,
    blur: int = 16,
) -> None:
    """给主按钮挂 hover 紫外发光（模拟萤火虫微光）."""
    watcher = _HoverGlowFilter(button, glow, blur)
    button.installEventFilter(watcher)
    # 父子关系保证 watcher 生命周期与按钮一致
    watcher.setParent(button)


__all__ = [
    "BreathingDot",
    "ThinkingDots",
    "StatusLamp",
    "ToggleSwitch",
    "fade_rise_in",
    "press_bounce",
    "purple_shadow",
    "attach_hover_glow",
]
