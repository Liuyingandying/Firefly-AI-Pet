# -*- coding: utf-8 -*-
"""VoiceOrb — 语音交互状态圆球（纯展示组件，ui美化暖纸视觉）.

蓝紫顶部 + 淡蓝底部 + 柔和白雾 + 轻光晕的圆球，按派生状态呈现
待机呼吸 / 聆听起伏 / 处理流动 / 说话起伏 / 异常提示 / 静音变暗 /
连接中脉冲 / 会话结束。

设计约束（见需求）：
- 只做渲染与动画，不含任何语音/Agent 业务逻辑；状态由宿主通过
  ``set_connection`` 等方法注入，音量电平经 Qt 信号跨线程进入（真实
  采集/播放电平），组件自身绝不生成或模拟数据。
- 连接状态、麦克风静音、用户发言、播放、任务各自独立注入，派生显示
  优先级：连接异常 > 会话结束 > 异常 > 静音 > 说话 > 处理 > 聆听 >
  执行 > 待机。
- 尊重系统"减少动态效果"偏好（Windows 客户区动画设置）；不可见时
  停动画定时器，卸载时自动清理。
"""

from __future__ import annotations

import ctypes
import math

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

# 连接状态
CONN_DISCONNECTED = "disconnected"
CONN_CONNECTING = "connecting"
CONN_RECONNECTING = "reconnecting"
CONN_READY = "ready"
CONN_ENDED = "ended"

# 派生显示状态
ST_IDLE = "idle"            # 待机
ST_LISTENING = "listening"  # 聆听（检测到用户说话）
ST_PROCESSING = "processing"  # 处理（等待响应）
ST_EXECUTING = "executing"  # 执行（Agent 工具/任务）
ST_SPEAKING = "speaking"    # 说话（真实播放中）
ST_MUTED = "muted"          # 静音
ST_ERROR = "error"          # 异常
ST_CONNECTING = "connecting"
ST_RECONNECTING = "reconnecting"
ST_ENDED = "ended"

_STATUS_TEXT = {
    ST_IDLE: "待机",
    ST_LISTENING: "正在聆听",
    ST_PROCESSING: "正在思考",
    ST_EXECUTING: "正在执行",
    ST_SPEAKING: "正在说话",
    ST_MUTED: "已静音",
    ST_ERROR: "连接异常",
    ST_CONNECTING: "连接中…",
    ST_RECONNECTING: "重连中…",
    ST_ENDED: "会话已结束",
}

_SPI_GETCLIENTAREAANIMATION = 0x1042


def system_reduced_motion() -> bool:
    """读取 Windows「在 Windows 中显示动画」(客户区动画) 系统偏好。"""
    buf = ctypes.c_int(1)
    if ctypes.windll.user32.SystemParametersInfoW(
        _SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(buf), 0
    ):
        return not bool(buf.value)
    return False


class VoiceOrb(QWidget):
    """状态圆球。所有状态注入方法幂等；电平经信号跨线程进入。"""

    #: 音频线程推送的真实用户麦克风电平 [0,1]（经队列进入 GUI 线程）
    user_level_received = Signal(float)
    #: 音频线程推送的真实播放电平 [0,1]（当前链路以会话轮询派生）
    output_level_received = Signal(float)

    ORB = 96  # 圆球直径（逻辑像素）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.ORB + 24, self.ORB + 24)

        # --- 注入态（业务真相，互相独立） -----------------------------
        self._connection = CONN_DISCONNECTED
        self._error: str | None = None
        self._muted = False
        self._mic_open = False
        self._user_talking = False
        self._processing = False
        self._task_active = False
        self._task_text = ""
        self._speaking = False
        self._user_level = 0.0   # 平滑后的真实电平
        self._output_level = 0.0

        # --- 动画相位 ------------------------------------------------
        self._phase = 0.0
        self._motion_reduced = system_reduced_motion()
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30fps，仅可见时运行
        self._timer.timeout.connect(self._tick)
        self.user_level_received.connect(self._on_user_level)
        self.output_level_received.connect(self._on_output_level)

    # ------------------------------------------------------------ 注入 API

    def set_connection(self, state: str) -> None:
        if state != self._connection:
            self._connection = state
            self._restart()
            self.update()

    def set_error(self, message: str | None) -> None:
        if message != self._error:
            self._error = message
            self.update()

    def set_mic_muted(self, muted: bool) -> None:
        if muted != self._muted:
            self._muted = muted
            self.update()

    def set_mic_open(self, open_: bool) -> None:
        """麦克风采集已开启 ≠ 正在聆听；安静时仍显示待机。"""
        if open_ != self._mic_open:
            self._mic_open = open_
            self.update()

    def set_processing(self, active: bool) -> None:
        if active != self._processing:
            self._processing = active
            self.update()

    def set_task(self, active: bool, text: str = "") -> None:
        if active != self._task_active or text != self._task_text:
            self._task_active = active
            self._task_text = text
            self.update()

    def set_speaking(self, active: bool) -> None:
        if active != self._speaking:
            self._speaking = active
            if not active:
                self._output_level = 0.0
            self.update()

    # ------------------------------------------------------------ 电平注入

    def push_user_level(self, value: float) -> None:
        """任意线程可调；经信号队列到 GUI 线程。"""
        self.user_level_received.emit(max(0.0, min(1.0, float(value))))

    def push_output_level(self, value: float) -> None:
        self.output_level_received.emit(max(0.0, min(1.0, float(value))))

    def _on_user_level(self, value: float) -> None:
        self._user_level = value
        talking = value > 0.06
        if talking != self._user_talking:
            self._user_talking = talking
            self.update()

    def _on_output_level(self, value: float) -> None:
        self._output_level = value
        self.update()

    # ------------------------------------------------------------ 派生状态

    def derived_state(self) -> str:
        if self._connection == CONN_CONNECTING:
            return ST_CONNECTING
        if self._connection == CONN_RECONNECTING:
            return ST_RECONNECTING
        if self._connection == CONN_ENDED:
            return ST_ENDED
        if self._error is not None:
            return ST_ERROR
        if self._connection != CONN_READY:
            return ST_ERROR if self._error else ST_IDLE
        if self._muted and not self._speaking:
            return ST_MUTED
        if self._speaking:
            return ST_SPEAKING
        if self._processing:
            return ST_PROCESSING
        if self._mic_open and self._user_talking and not self._muted:
            return ST_LISTENING
        if self._task_active:
            return ST_EXECUTING
        return ST_IDLE

    def status_text(self) -> str:
        state = self.derived_state()
        if state == ST_EXECUTING and self._task_text:
            return f"正在执行 · {self._task_text}"
        return _STATUS_TEXT.get(state, state)

    # ------------------------------------------------------------ 渲染

    def _tick(self) -> None:
        self._phase = (self._phase + 1.0) % (1 << 30)
        self.update()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._restart()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        # 不可见即停动画，省电且满足"页面不可见停止无用动画"。
        self._timer.stop()
        super().hideEvent(event)

    def _restart(self) -> None:
        animated = not self._motion_reduced or self.derived_state() in (
            ST_CONNECTING, ST_RECONNECTING, ST_SPEAKING, ST_LISTENING,
        )
        if animated and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        if event.type() == QEvent.DevicePixelRatioChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        state = self.derived_state()
        cx = self.width() / 2
        cy = self.height() / 2

        breath = math.sin(self._phase * 0.055)  # 待机呼吸 ~3.8s 周期
        level = max(self._user_level if state == ST_LISTENING else 0.0,
                    self._output_level if state == ST_SPEAKING else 0.0)

        # 半径：呼吸/电平起伏
        base = self.ORB / 2
        radius = base
        if state in (ST_IDLE, ST_MUTED, ST_CONNECTING, ST_RECONNECTING):
            radius *= 1.0 + 0.025 * breath
        radius *= 1.0 + 0.10 * level

        dim = state in (ST_MUTED, ST_ENDED)
        error = state == ST_ERROR
        connecting = state in (ST_CONNECTING, ST_RECONNECTING)

        # --- 光晕 ------------------------------------------------------
        halo = QRadialGradient(cx, cy, radius * 1.9)
        if error:
            halo_c = QColor(255, 120, 120, 46)
        elif connecting:
            halo_c = QColor(140, 160, 255, int(56 + 34 * breath))
        elif dim:
            halo_c = QColor(150, 160, 190, 22)
        else:
            halo_c = QColor(140, 160, 255, int(40 + 26 * level))
        halo.setColorAt(0.0, halo_c)
        halo.setColorAt(1.0, QColor(140, 160, 255, 0))
        painter.setBrush(halo)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(cx, cy, radius * 3.8, radius * 3.8)

        # --- 主体：上蓝紫下淡蓝的纵向渐变 -------------------------------
        body = _BodyGradient(cx, cy, radius, dim, error)
        painter.setBrush(body)
        painter.drawEllipse(cx, cy, radius * 2, radius * 2)

        # --- 白色云雾（两层柔光斑块，随相位缓漂；电平加大流动） ---------
        speed = 1.0
        if state == ST_PROCESSING:
            speed = 2.2
        elif state == ST_SPEAKING:
            speed = 1.4 + 2.2 * level
        elif state == ST_LISTENING:
            speed = 1.0 + 1.6 * level
        drift = self._phase * 0.011 * (0.35 if self._motion_reduced else speed)
        cloud_alpha = 88 if not (dim or error) else 40
        for i, (kx, ky, kr) in enumerate(((0.0, 0.28, 0.62), (0.30, 0.16, 0.40))):
            ang = drift + i * 2.1
            ox = cx + math.cos(ang) * radius * 0.22 * kx * 2
            oy = cy - radius * ky + math.sin(ang * 0.8) * radius * 0.06
            blob = QRadialGradient(ox, oy, radius * kr)
            blob.setColorAt(0.0, QColor(255, 255, 255, cloud_alpha))
            blob.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(blob)
            painter.drawEllipse(ox, oy, radius * kr * 2, radius * kr * 2)

        # --- 处理中：上部流动弧光；连接中：脉冲环 ----------------------
        if state == ST_PROCESSING:
            pen = painter.pen()
            from PySide6.QtGui import QPen

            arc_a = QColor(255, 255, 255, 150)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(arc_a, 2.4))
            start = int((self._phase * 7) % 360) * 16
            painter.drawArc(cx, cy, radius * 1.7, radius * 1.7, start, 100 * 16)
            painter.setPen(QPen(QColor(255, 255, 255, 70), 2.4))
            painter.drawArc(cx, cy, radius * 1.7, radius * 1.7, start + 180 * 16, 80 * 16)
            painter.setPen(pen)
        elif connecting:
            from PySide6.QtGui import QPen

            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(140, 160, 255, int(120 + 90 * breath)), 2.0))
            ring = radius * (2.1 + 0.12 * breath)
            painter.drawEllipse(cx, cy, ring, ring)

        # --- 静音斜杠 / 异常叹号 --------------------------------------
        if state == ST_MUTED:
            from PySide6.QtGui import QPen

            painter.setPen(QPen(QColor(120, 128, 160, 190), 3.0))
            r = radius * 0.55
            painter.drawLine(cx - r, cy + r, cx + r, cy - r)
        elif error:
            painter.setPen(QColor(210, 70, 70))
            f = QFont(self.font())
            f.setPixelSize(int(radius * 0.9))
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(painter.window(), Qt.AlignCenter, "!")


class _BodyGradient(QRadialGradient):
    """上蓝紫下淡蓝的主体渐变；静音/结束降饱和，异常偏警示色。"""

    def __init__(self, cx: float, cy: float, radius: float, dim: bool, error: bool) -> None:
        super().__init__(cx, cy - radius * 0.35, radius * 1.75, cx, cy)
        if error:
            top, mid, bottom = QColor(226, 130, 130), QColor(240, 190, 190), QColor(250, 226, 226)
        elif dim:
            top, mid, bottom = QColor(168, 176, 214), QColor(198, 210, 236), QColor(226, 234, 246)
        else:
            top, mid, bottom = QColor(126, 134, 255), QColor(168, 196, 255), QColor(214, 234, 255)
        self.setColorAt(0.0, top)
        self.setColorAt(0.55, mid)
        self.setColorAt(1.0, bottom)


__all__ = [
    "VoiceOrb",
    "system_reduced_motion",
    "CONN_DISCONNECTED", "CONN_CONNECTING", "CONN_RECONNECTING",
    "CONN_READY", "CONN_ENDED",
    "ST_IDLE", "ST_LISTENING", "ST_PROCESSING", "ST_EXECUTING",
    "ST_SPEAKING", "ST_MUTED", "ST_ERROR", "ST_CONNECTING",
    "ST_RECONNECTING", "ST_ENDED",
]
