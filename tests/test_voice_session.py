# -*- coding: utf-8 -*-
"""VoiceSessionController 测试：假体注入，不碰真实网络/声卡。

覆盖：连接探测、说话生命周期、Agent 任务转发、静音关流、
插话打断（真实 stop 调用）、发言结束信号、会话结束清理。
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "plugins" / "firefly_voice_chat"))

import voice_ui.voice_session as vs
from voice_ui.voice_session import VoiceSessionController


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeConfig:
    url = "http://127.0.0.1:8300"


class _FakeClient:
    def __init__(self, speak_delay: float = 0.0, speak_fail: bool = False):
        self.config = _FakeConfig()
        self.speak_calls: list[str] = []
        self.stop_calls = 0
        self._delay = speak_delay
        self._fail = speak_fail

    def speak(self, text: str, emotion=None, priority=1):
        self.speak_calls.append(text)
        if self._fail:
            raise RuntimeError("connection refused")
        if self._delay:
            time.sleep(self._delay)

    def stop(self):
        self.stop_calls += 1


class _FakeStream:
    def __init__(self):
        self.stopped = False
        self.closed = False

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _wait(signal, timeout_ms=2000):
    """等待 Qt 信号触发一次（线程经队列投递）。"""
    loop = QEventLoop()
    got = []

    def _capture(*args):
        got.append(args)
        loop.quit()

    signal.connect(_capture)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    try:
        signal.disconnect(_capture)
    except RuntimeError:
        pass
    return got[0] if got else None


def _drain(qapp, ms=250):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_start_probes_health_and_becomes_ready(qapp):
    client = _FakeClient()
    session = VoiceSessionController(
        client=client,
        health_probe=lambda: {"status": "ok"},
        queue_poll=lambda: {"playing": None},
        mic_stream_factory=lambda cb: _FakeStream(),
    )
    got = _wait(session.connection_changed, 1500)
    session.start()
    _wait(session.connection_changed, 1500)
    _drain(qapp)
    assert session.status_text() != ""
    session.stop()
    assert client.stop_calls == 1


def test_speak_lifecycle_drives_speaking_state(qapp):
    client = _FakeClient(speak_delay=0.15)
    session = VoiceSessionController(
        client=client,
        health_probe=lambda: {"status": "ok"},
        queue_poll=lambda: {"playing": None},
    )
    states: list[bool] = []
    session.speaking_changed.connect(states.append)
    session.start()
    _drain(qapp, 150)
    session.speak("你好")
    _drain(qapp, 600)
    session.stop()
    assert client.speak_calls == ["你好"]
    assert states and states[0] is True and states[-1] is False


def test_speak_failure_sets_error_and_reconnecting(qapp):
    client = _FakeClient(speak_fail=True)
    probes = {"n": 0}

    def failing_probe():
        probes["n"] += 1
        if probes["n"] == 1:
            return {"status": "ok"}  # 初始探测成功 → 已就绪
        return {"status": "error"}  # speak 失败后的重探也失败 → 异常态

    session = VoiceSessionController(
        client=client,
        health_probe=failing_probe,
        queue_poll=lambda: {"playing": None},
    )
    session.start()
    _drain(qapp, 150)
    assert session._error is None  # 初始探测成功 → 已就绪
    session.speak("你好")
    _drain(qapp, 600)
    assert session._error is not None
    session.stop()


def test_agent_events_drive_task_state(qapp):
    from core.agent_events import AgentEvent, AgentEventType

    session = VoiceSessionController(
        client=_FakeClient(),
        health_probe=lambda: {"status": "ok"},
        queue_poll=lambda: {"playing": None},
    )
    session.start()
    _drain(qapp, 100)
    session.notify_agent_event(
        AgentEvent.make("test", AgentEventType.STATUS, status="working")
    )
    assert session._task_active and session._task_text == "working"
    assert session.status_text().startswith("正在执行")
    session.notify_agent_event(AgentEvent.make("test", AgentEventType.FINAL, text="done"))
    assert not session._task_active
    session.stop()


def test_mute_closes_mic_stream(qapp):
    streams = []

    def factory(cb):
        s = _FakeStream()
        streams.append(s)
        return s

    session = VoiceSessionController(
        client=_FakeClient(),
        health_probe=lambda: {"status": "ok"},
        queue_poll=lambda: {"playing": None},
        mic_stream_factory=factory,
    )
    session.start()
    _drain(qapp, 150)
    assert streams and session._mic_stream is not None
    session.set_muted(True)
    assert streams[0].stopped and streams[0].closed
    assert session._mic_stream is None  # 静音 = 停止采集，而非忽略数据
    session.set_muted(False)
    assert session._mic_stream is not None
    session.stop()


def test_barge_in_stops_playback_once_per_utterance(qapp, monkeypatch):
    client = _FakeClient()
    session = VoiceSessionController(
        client=client,
        health_probe=lambda: {"status": "ok"},
        queue_poll=lambda: {"playing": None},
    )
    session._active = True
    session._queue_playing = True  # 服务端正在播放（真实轮询结果）
    session._mic_stream = _FakeStream()  # 麦克风已开启（采集回调才生效）
    clock = {"t": 1000.0}
    monkeypatch.setattr(vs, "_now", lambda: clock["t"])

    import numpy as np

    loud = (np.ones((800, 1), dtype="int16") * 8000).reshape(800, 1)
    quiet = (np.zeros((800, 1), dtype="int16")).reshape(800, 1)

    session._on_audio_block(loud, 800, None, None)  # 用户插话
    _drain(qapp, 300)
    assert client.stop_calls == 1  # 打断：停止当前播放

    clock["t"] += 0.1
    session._on_audio_block(loud, 800, None, None)
    _drain(qapp, 300)
    assert client.stop_calls == 1  # 同一轮发言不重复打断

    clock["t"] += 2.0  # 静默超时 → 一轮发言结束
    session._on_audio_block(quiet, 800, None, None)
    _drain(qapp, 300)
    session._queue_playing = True  # 新一轮回复又开始播放
    clock["t"] += 0.1
    session._on_audio_block(loud, 800, None, None)
    _drain(qapp, 300)
    assert client.stop_calls == 2  # 新发言可再次打断新播放


def test_utterance_end_emitted_after_silence(qapp, monkeypatch):
    session = VoiceSessionController(
        client=_FakeClient(),
        health_probe=lambda: {"status": "ok"},
        queue_poll=lambda: {"playing": None},
    )
    session._active = True
    session._mic_stream = _FakeStream()  # 麦克风已开启
    got: list[float] = []
    session.utterance_end.connect(got.append)

    clock = {"t": 0.0}
    monkeypatch.setattr(vs, "_now", lambda: clock["t"])

    import numpy as np

    loud = (np.ones((800, 1), dtype="int16") * 8000).reshape(800, 1)
    quiet = (np.zeros((800, 1), dtype="int16")).reshape(800, 1)

    session._on_audio_block(loud, 800, None, None)
    clock["t"] += 0.2
    session._on_audio_block(loud, 800, None, None)
    clock["t"] += 1.2  # > 0.9s 静默窗
    session._on_audio_block(quiet, 800, None, None)
    _drain(qapp, 200)
    assert got and got[0] > 0


def test_stop_ends_session_and_stops_playback(qapp):
    client = _FakeClient()
    session = VoiceSessionController(
        client=client,
        health_probe=lambda: {"status": "ok"},
        queue_poll=lambda: {"playing": None},
    )
    ended = _wait(session.connection_changed, 100)  # no-op 预热占位
    session.start()
    _drain(qapp, 120)
    session.stop()
    _drain(qapp, 200)
    assert not session._active
    assert client.stop_calls >= 1
    assert session.status_text() == ""


def test_speech_buffer_receives_loud_blocks(qapp, monkeypatch):
    """回归：响块（真正说话）必须进录音缓冲，否则 STT 拿到纯静音。"""
    session = VoiceSessionController(
        client=_FakeClient(),
        health_probe=lambda: {"status": "ok"},
        queue_poll=lambda: {"playing": None},
    )
    session._active = True
    session._mic_stream = _FakeStream()
    clock = {"t": 0.0}
    monkeypatch.setattr(vs, "_now", lambda: clock["t"])

    import numpy as np

    loud = (np.ones((800, 1), dtype="int16") * 9000)

    for _ in range(6):  # 6 个响块（300ms）→ 发言中
        session._on_audio_block(loud, 800, None, None)
        clock["t"] += 0.05
    assert len(session._speech_buffer) == 6, "响块未进缓冲"

    for _ in range(3):  # 3 个安静块（尾部）
        session._on_audio_block(np.zeros((800, 1), dtype="int16"), 800, None, None)
        clock["t"] += 0.05
    assert len(session._speech_buffer) == 9

    clock["t"] += 1.0  # 静默超时 → 一轮结束，缓冲交给 STT 并清空
    session._on_audio_block(np.zeros((800, 1), dtype="int16"), 800, None, None)
    assert session._speech_buffer == []
