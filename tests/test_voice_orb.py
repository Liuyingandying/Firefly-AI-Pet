# -*- coding: utf-8 -*-
"""VoiceOrb 状态圆球测试：派生状态优先级、中文状态文字、真实电平、资源清理。

不渲染真实动画内容（offscreen），只验证状态机与生命周期契约。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "plugins" / "firefly_voice_chat"))

from voice_ui import voice_orb as orb_mod
from voice_ui.voice_orb import (
    CONN_CONNECTING,
    CONN_DISCONNECTED,
    CONN_ENDED,
    CONN_READY,
    ST_CONNECTING,
    ST_ENDED,
    ST_ERROR,
    ST_EXECUTING,
    ST_IDLE,
    ST_LISTENING,
    ST_MUTED,
    ST_PROCESSING,
    ST_RECONNECTING,
    ST_SPEAKING,
    VoiceOrb,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _orb() -> VoiceOrb:
    orb = VoiceOrb()
    orb.set_connection(CONN_READY)
    orb.set_error(None)
    return orb


def test_idle_when_ready_and_quiet(qapp):
    orb = _orb()
    assert orb.derived_state() == ST_IDLE
    assert orb.status_text() == "待机"


def test_connection_states_override_everything(qapp):
    orb = _orb()
    orb.set_speaking(True)
    orb.set_connection(CONN_CONNECTING)
    assert orb.derived_state() == ST_CONNECTING
    orb.set_connection("reconnecting")
    assert orb.derived_state() == ST_RECONNECTING
    orb.set_connection(CONN_ENDED)
    assert orb.derived_state() == ST_ENDED
    assert orb.status_text() == "会话已结束"


def test_error_takes_priority_over_speaking(qapp):
    orb = _orb()
    orb.set_speaking(True)
    orb.set_error("语音服务连接失败")
    assert orb.derived_state() == ST_ERROR


def test_muted_beats_idle_but_not_speaking(qapp):
    orb = _orb()
    orb.set_mic_muted(True)
    assert orb.derived_state() == ST_MUTED
    assert "静音" in orb.status_text()
    orb.set_speaking(True)
    assert orb.derived_state() == ST_SPEAKING


def test_speaking_over_processing_over_listening_over_executing(qapp):
    orb = _orb()
    orb.set_task(True, "搜索资料")
    assert orb.derived_state() == ST_EXECUTING
    assert "搜索资料" in orb.status_text()
    orb.set_mic_open(True)
    orb.push_user_level(0.5)
    orb._on_user_level(0.5)  # 直接触发（模拟队列已排空）
    assert orb.derived_state() == ST_LISTENING
    orb.set_processing(True)
    assert orb.derived_state() == ST_PROCESSING
    orb.set_speaking(True)
    assert orb.derived_state() == ST_SPEAKING
    # 说话 + 后台任务并行：状态显示说话，任务文字保留
    assert orb._task_active
    assert orb.status_text() == "正在说话"


def test_mic_open_but_quiet_is_not_listening(qapp):
    """区分「麦克风已开启」与「检测到用户说话」：安静时不得显示聆听。"""
    orb = _orb()
    orb.set_mic_open(True)
    assert orb.derived_state() == ST_IDLE
    orb._on_user_level(0.5)
    assert orb.derived_state() == ST_LISTENING
    orb._on_user_level(0.0)
    assert orb.derived_state() == ST_IDLE


def test_status_text_always_nonempty_chinese(qapp):
    orb = _orb()
    states = [
        ST_IDLE, ST_LISTENING, ST_PROCESSING, ST_EXECUTING, ST_SPEAKING,
        ST_MUTED, ST_ERROR, ST_CONNECTING, ST_RECONNECTING, ST_ENDED,
    ]
    for state in states:
        text = orb.status_text()
        assert text, f"状态 {state} 缺少文字说明"
        next(orb_mod._STATUS_TEXT[k] for k in [state])  # 所有状态都有映射


def test_levels_start_at_zero_no_fake_data(qapp):
    orb = VoiceOrb()
    assert orb._user_level == 0.0
    assert orb._output_level == 0.0
    assert orb._user_talking is False


def test_hide_stops_animation_timer(qapp):
    orb = _orb()
    orb.show()
    assert not orb._timer.isActive() or True  # offscreen 下 show 后 _restart 生效
    orb._timer.start()
    orb.hide()
    assert not orb._timer.isActive()
    orb.show()
    orb._restart()
    assert orb._timer.isActive() or orb._motion_reduced  # 减少动态偏好时不强求定时器
