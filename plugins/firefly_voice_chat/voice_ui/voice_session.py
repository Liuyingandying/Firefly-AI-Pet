# -*- coding: utf-8 -*-
"""VoiceCompanion — 「语音陪伴」会话编排（真实事件驱动，无任何模拟数据）.

点击输入区「语音」后开启与流萤的语音陪伴模式。职责：

- 连接状态：会话开启 / 每 30s / 请求失败后探测 ``GET /health``（真实 HTTP），
  未连接绝不显示已就绪；失败进入重连中并给出重试入口。
- 说话状态：由两路**真实事件**共同判定——(1) 本会话发起的 ``speak`` HTTP
  调用生命周期（服务端 ``play=true`` 在播放完成后才返回，调用窗口即播放
  窗口）；(2) 轮询 ``GET /voice/queue`` 的 ``playing`` 字段（覆盖播放按钮 /
  全局自动播报发起的播放）。服务端播放无电平流，说话态不伪造音量。
- 聆听状态：sounddevice 麦克风采集真实 RMS 电平 → 圆球起伏；阈值 + 迟滞
  判定「正在说话 / 一轮发言结束」。安静时回落待机，不常驻聆听。
- 静音：独立状态；开启后**关闭麦克风采集流**（非仅忽略数据）。
- 打断：说话中检测到用户发言 → 调用 ``POST /voice/queue/stop`` 停止当前
  播放并清理队列（现有语音链路支持），回到聆听；后台任务不受影响。
- 任务状态：宿主转发 AgentEvent（STATUS=执行中，FINAL/ERROR/CANCELLED=
  结束）；可与说话并行，圆球显示说话、任务文字保留在旁。
- 语音识别（STT）：发言期间缓冲原始 PCM，一轮结束后 POST 语音服务
  ``POST /voice/stt``（faster-whisper 本机推理），识别文本经
  ``utterance_text`` 交宿主进入对话链路；服务不可用（501/网络）时
  经 ``stt_unavailable`` 一次性提示并降级为「仅朗读」模式。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

log = logging.getLogger("firefly.voice.companion")

try:  # 麦克风采样的 RMS 计算依赖 numpy（宿主环境已带）
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

try:  # 麦克风采集为可选能力：缺失时聆听/打断不可用，其余状态照常
    import sounddevice as _sd
except Exception:  # noqa: BLE001
    _sd = None

_POLL_INTERVAL_MS = 800
_HEALTH_INTERVAL_MS = 30_000
_SPEECH_START_LEVEL = 0.06
_SPEECH_END_LEVEL = 0.03
_SPEECH_END_SILENCE_S = 0.9
_SAMPLE_RATE = 16_000
_BLOCK_MS = 50


def _now() -> float:
    return time.monotonic()


class VoiceSessionController(QObject):
    """语音陪伴会话的状态编排。所有对外状态变化都发 Qt 信号。"""

    connection_changed = Signal(str)          # voice_orb.CONN_*
    error_changed = Signal(object)            # str | None
    speaking_changed = Signal(bool)
    task_changed = Signal(bool, str)
    mic_open_changed = Signal(bool)
    muted_changed = Signal(bool)
    status_text_changed = Signal(str)
    #: 用户一轮发言结束（携带实际时长；文本经 STT 后走 utterance_text）
    utterance_end = Signal(float)
    #: STT 识别出的用户发言文本（语音服务 /voice/stt，真实转写）
    utterance_text = Signal(str)
    #: STT 不可用（服务 501 / 网络失败）——一次性提示用
    stt_unavailable = Signal(str)

    def __init__(
        self,
        client: Any = None,
        parent: QObject | None = None,
        *,
        health_probe: Callable[[], dict] | None = None,
        queue_poll: Callable[[], dict] | None = None,
        mic_stream_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        if client is None:
            from voice_client.client import get_shared_client

            client = get_shared_client()
        self._client = client
        # 可注入的真实数据源（测试用假体；生产走 HTTP/声卡）
        self._health_probe = health_probe
        self._queue_poll = queue_poll
        self._mic_stream_factory = mic_stream_factory

        self._active = False
        self._muted = False
        self._speaking = False            # 本会话发起的 speak 调用窗口
        self._queue_playing = False       # 队列轮询的服务端真相
        self._processing = False
        self._task_active = False
        self._task_text = ""
        self._error: str | None = None

        self._mic_stream: Any = None
        self._speech_talking = False
        self._last_loud_at = 0.0
        self._barge_in_sent = False
        self._lock = threading.Lock()
        self._level_target: Callable[[float], None] | None = None
        self._speech_buffer: list[Any] = []   # 发言期间的原始 int16 块（16k 单声道）
        self._stt_ok: bool | None = None      # None=未探测；False=服务端 501

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_queue_once)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(_HEALTH_INTERVAL_MS)
        self._health_timer.timeout.connect(lambda: self._probe_health(False))

    # ------------------------------------------------------------ 会话开关

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self.connection_changed.emit("connecting")
        self._open_mic()
        self._poll_timer.start()
        self._health_timer.start()
        self._probe_health(True)

    def stop(self) -> None:
        """结束会话：停采集、停播放、停轮询，连接态置为已结束。"""
        if not self._active:
            return
        self._active = False
        self._poll_timer.stop()
        self._health_timer.stop()
        self._close_mic()
        self._processing = False
        self._task_active = False
        self.task_changed.emit(False, "")
        self._spawn(self._stop_playback)
        self.speaking_changed.emit(False)
        self.connection_changed.emit("ended")
        self.status_text_changed.emit(self.status_text())

    @property
    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------ 宿主转发

    def notify_ask_submitted(self) -> None:
        """一轮用户发言已提交（键盘输入或未来 STT），等待响应。"""
        if not self._active:
            return
        self._processing = True
        self.status_text_changed.emit(self.status_text())

    def notify_reply_done(self, reply_text: str = "") -> None:
        """收到最终回复：处理结束；陪伴模式下自动朗读（真实 TTS 链路）。"""
        self._processing = False
        if self._active and reply_text.strip():
            self.speak(reply_text)
        self.status_text_changed.emit(self.status_text())

    def notify_agent_event(self, event: Any) -> None:
        """转发宿主 AgentEvent → 执行（任务）状态。"""
        from core.agent_events import AgentEventType

        et = getattr(event, "type", None)
        if et == AgentEventType.STATUS:
            status = getattr(event, "status", "") or ""
            if status and status not in ("idle", "success", "error"):
                self._task_active = True
                self._task_text = status
                self.task_changed.emit(True, status)
                self.status_text_changed.emit(self.status_text())
        elif et in (AgentEventType.FINAL, AgentEventType.ERROR, AgentEventType.CANCELLED):
            if self._task_active:
                self._task_active = False
                self._task_text = ""
                self.task_changed.emit(False, "")
                self.status_text_changed.emit(self.status_text())

    # ------------------------------------------------------------ 说话

    def speak(self, text: str) -> None:
        """经真实语音链路朗读（服务端播放完成后调用才返回）。"""
        def _run() -> None:
            self._speaking = True
            self.speaking_changed.emit(True)
            self.status_text_changed.emit(self.status_text())
            try:
                self._client.speak(text)
            except Exception as exc:  # noqa: BLE001 — client 已不抛，双保险
                log.warning("companion speak failed: %s", exc)
                self.error_changed.emit("语音服务连接失败")
                self.connection_changed.emit("reconnecting")
                self._probe_health(False)
            finally:
                self._speaking = False
                self.speaking_changed.emit(False)
                self.status_text_changed.emit(self.status_text())

        self._spawn(_run)

    def interrupt_playback(self) -> None:
        """打断：停止当前播放并清空待播队列（真实服务端操作）。"""
        self._spawn(self._stop_playback)

    def _stop_playback(self) -> None:
        try:
            self._client.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("companion stop failed: %s", exc)
        self._queue_playing = False
        self.speaking_changed.emit(False)
        self.status_text_changed.emit(self.status_text())

    # ------------------------------------------------------------ 静音

    def set_muted(self, muted: bool) -> None:
        if muted == self._muted:
            return
        self._muted = muted
        self.muted_changed.emit(muted)
        if muted:
            self._close_mic()
        else:
            self._open_mic()
        self.status_text_changed.emit(self.status_text())

    @property
    def muted(self) -> bool:
        return self._muted

    # ------------------------------------------------------------ 派生文字

    def status_text(self) -> str:
        if not self._active:
            return ""
        if self._error is not None:
            return "连接异常 · 将自动重连"
        if self._speaking or self._queue_playing:
            return "正在说话"
        if self._processing:
            return "正在思考"
        if self._task_active:
            return f"正在执行 · {self._task_text}" if self._task_text else "正在执行"
        if self._muted:
            return "已静音"
        if self._speech_talking:
            return "正在聆听"
        return "待机 · 请说话"

    # ------------------------------------------------------------ 连接探测

    def _probe_health(self, initial: bool) -> None:
        def _run() -> None:
            ok = False
            try:
                if self._health_probe is not None:
                    data = self._health_probe()
                else:
                    url = str(getattr(self._client.config, "url", "http://127.0.0.1:8300"))
                    with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=2.0) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                ok = bool(data.get("status") == "ok")
            except Exception as exc:  # noqa: BLE001
                log.debug("voice health probe failed: %s", exc)
            if not self._active:
                return
            if ok:
                self._error = None
                self.connection_changed.emit("ready")
            else:
                # 探测失败：会话初始失败与运行中失败都进入异常态（重连中）。
                self._error = "语音服务未运行" if initial else "语音服务连接失败"
                self.connection_changed.emit("reconnecting" if not initial else "connecting")
            self.error_changed.emit(self._error)
            self.status_text_changed.emit(self.status_text())

        self._spawn(_run)

    def _poll_queue_once(self) -> None:
        def _run() -> None:
            playing = False
            try:
                if self._queue_poll is not None:
                    data = self._queue_poll()
                else:
                    url = str(getattr(self._client.config, "url", "http://127.0.0.1:8300"))
                    with urllib.request.urlopen(f"{url.rstrip('/')}/voice/queue", timeout=1.5) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                playing = data.get("playing") is not None
            except Exception:  # noqa: BLE001 — 轮询失败不打扰状态
                return
            if playing != self._queue_playing:
                self._queue_playing = playing
                self.speaking_changed.emit(self._speaking or playing)
                self.status_text_changed.emit(self.status_text())

        self._spawn(_run)

    # ------------------------------------------------------------ 麦克风

    def _open_mic(self) -> None:
        if self._mic_stream is not None or self._muted:
            return
        if _sd is None:
            log.info("sounddevice 不可用：聆听/打断不可用（STT 亦未接入）")
            return
        try:
            if self._mic_stream_factory is not None:
                self._mic_stream = self._mic_stream_factory(self._on_audio_block)
            else:
                self._mic_stream = _sd.InputStream(
                    samplerate=_SAMPLE_RATE, channels=1, dtype="int16",
                    blocksize=_SAMPLE_RATE * _BLOCK_MS // 1000,
                    callback=self._on_audio_block,
                )
                self._mic_stream.start()
            self.mic_open_changed.emit(True)
        except Exception as exc:  # noqa: BLE001 — 无麦克风/权限拒绝不影响其余状态
            log.warning("麦克风打开失败: %s", exc)
            self._mic_stream = None
            self.mic_open_changed.emit(False)

    def _close_mic(self) -> None:
        stream, self._mic_stream = self._mic_stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.mic_open_changed.emit(False)
        with self._lock:
            self._speech_talking = False
            self._barge_in_sent = False

    def _on_audio_block(self, indata, frames, time_info, status) -> None:
        """声卡回调线程：真实 RMS 电平 → 圆球 + 发言缓冲/打断判定。"""
        with self._lock:
            if self._mic_stream is None or self._muted or not self._active or _np is None:
                return
            rms = float(_np.sqrt(_np.mean(indata.astype("float64") ** 2))) / 32768.0
            level = min(1.0, rms * 8.0)  # 线性增益到 [0,1] 显示区间
            target = self._level_target
            starts = level > _SPEECH_START_LEVEL and not self._speech_talking
            # 发言缓冲必须收「所有」块——尤其响块（真正说话的部分）；
            # 只收安静块会让 STT 拿到纯静音，识别为空、永远派发不出去。
            if (self._speech_talking or starts) and len(self._speech_buffer) < 600:
                self._speech_buffer.append(indata.copy())
        if target is not None:
            target(level)
        now = _now()
        with self._lock:
            if level > _SPEECH_START_LEVEL:
                self._last_loud_at = now
                if not self._speech_talking:
                    self._speech_talking = True
                    self._barge_in_sent = False
                    self.status_text_changed.emit(self.status_text())
            elif self._speech_talking and now - self._last_loud_at > _SPEECH_END_SILENCE_S:
                self._speech_talking = False
                self.status_text_changed.emit(self.status_text())
                duration = now - self._last_loud_at
                # 发言缓冲交给 STT（≥0.5s 才值得识别）
                buffered, self._speech_buffer = self._speech_buffer, []
                if duration >= 0.5 and buffered:
                    self._spawn(lambda: self._transcribe_buffer(buffered))
                self._barge_in_sent = False
                self.utterance_end.emit(max(0.0, duration))
            if (
                self._speech_talking
                and (self._speaking or self._queue_playing)
                and not self._barge_in_sent
            ):
                # 用户插话：停止当前播放（真实服务端 stop），回到聆听。
                self._barge_in_sent = True
                self._spawn(self._stop_playback)

    # ------------------------------------------------------------ STT

    def _transcribe_buffer(self, blocks: list[Any]) -> None:
        """发言缓冲 → WAV → POST /voice/stt（faster-whisper 本机推理）。"""
        import io
        import wave

        import numpy as _np

        audio = _np.concatenate(blocks, axis=0)  # int16 (N,1)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        wav = buf.getvalue()
        url = str(getattr(self._client.config, "url", "http://127.0.0.1:8300")).rstrip("/")
        try:
            import httpx

            resp = httpx.post(
                f"{url}/voice/stt",
                files={"file": ("utterance.wav", wav, "audio/wav")},
                data={"language": "zh"},
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("stt request failed: %s", exc)
            if self._stt_ok is not False:
                self._stt_ok = False
                self.stt_unavailable.emit(f"语音识别请求失败（{type(exc).__name__}）")
            return
        if resp.status_code == 501:
            if self._stt_ok is not False:
                self._stt_ok = False
                detail = str(resp.json().get("detail", ""))[:120]
                self.stt_unavailable.emit(f"语音识别未就绪：{detail}")
            return
        if resp.status_code != 200:
            if self._stt_ok is not False:
                self._stt_ok = False
                self.stt_unavailable.emit(f"语音识别失败（HTTP {resp.status_code}）")
            return
        self._stt_ok = True
        text = str(resp.json().get("text", "")).strip()
        if text:
            self.utterance_text.emit(text)

    # ------------------------------------------------------------ 基础设施

    def bind_level_target(self, target: Callable[[float], None] | None) -> None:
        """绑定圆球的电平入口（组件经 Qt 信号保证跨线程安全）。"""
        self._level_target = target

    def _spawn(self, fn) -> None:
        threading.Thread(target=fn, daemon=True, name="firefly-voice-companion").start()


class VoiceCompanionBar(QWidget):
    """输入区上方的陪伴条：圆球 + 中文状态文字 + 静音/结束操作。"""

    end_requested = Signal()

    def __init__(self, controller: VoiceSessionController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from ui import theme
        from voice_ui.voice_orb import VoiceOrb

        self._controller = controller
        self.orb = VoiceOrb(self)

        self._status = QLabel("连接中…", self)
        self._status.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY}; background: transparent; border: none;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )

        self._task = QLabel("", self)
        self._task.setStyleSheet(
            f"color: rgba{theme.V2.PRIMARY}; background: transparent; border: none;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION - 1}pt;"
        )
        self._task.setVisible(False)

        self._mute_btn = QPushButton("静音", self)
        self._mute_btn.setCursor(Qt.PointingHandCursor)
        self._mute_btn.setStyleSheet(
            f"QPushButton {{ color: rgba{theme.V2.TEXT_SECONDARY};"
            f" background: rgba{theme.V2.PRIMARY_SOFT}; border: none;"
            f" border-radius: 12px; padding: 4px 12px;"
            f" font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt; }}"
            f"QPushButton:hover {{ color: rgba{theme.V2.PRIMARY}; }}"
        )
        self._mute_btn.clicked.connect(self._toggle_mute)

        self._end_btn = QPushButton("结束语音", self)
        self._end_btn.setStyleSheet(self._mute_btn.styleSheet())
        self._end_btn.setCursor(Qt.PointingHandCursor)
        self._end_btn.clicked.connect(self.end_requested.emit)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.orb)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        col.addWidget(self._status)
        col.addWidget(self._task)
        row.addLayout(col, 1)
        row.addWidget(self._mute_btn)
        row.addWidget(self._end_btn)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(row)

        # 真实事件 → 组件
        controller.connection_changed.connect(self.orb.set_connection)
        controller.error_changed.connect(self.orb.set_error)
        controller.speaking_changed.connect(self.orb.set_speaking)
        controller.task_changed.connect(self._on_task)
        controller.muted_changed.connect(self.orb.set_mic_muted)
        controller.mic_open_changed.connect(self.orb.set_mic_open)
        controller.status_text_changed.connect(self._status.setText)
        controller.bind_level_target(self.orb.push_user_level)
        self._status.setText(controller.status_text())

    def _on_task(self, active: bool, text: str) -> None:
        self.orb.set_task(active, text)
        self._task.setText(f"后台任务：{text}" if active else "")
        self._task.setVisible(active)

    def _toggle_mute(self) -> None:
        self._controller.set_muted(not self._controller.muted)
        self._mute_btn.setText("取消静音" if self._controller.muted else "静音")


__all__ = ["VoiceSessionController", "VoiceCompanionBar"]
