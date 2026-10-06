# -*- coding: utf-8 -*-
"""v1.3.2 回归恢复测试: 按钮渲染 / 清洗 / Client / 取消 / 服务器存活。

运行:
    cd E:\\Firefly_AI_Pet
    .venv\\Scripts\\python.exe -m pytest tests/test_voice_v132_regression.py -v
"""

from __future__ import annotations

import os

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from voice_client import FireflyVoiceClient, PlayVoiceButton, remove_action_text  # noqa: E402
from voice_client.config import load_voice_config  # noqa: E402

BASE = "http://127.0.0.1:8300"


class _StubClient:
    def __init__(self, enabled=True):
        self.config = SimpleNamespace(auto_play=False, enabled=enabled, url="x")
        self.speak_calls: list[str] = []
        self.stop_calls = 0

    def speak(self, text, emotion=None, priority=1):
        self.speak_calls.append(text)
        return {"ok": True}

    def stop(self):
        self.stop_calls += 1
        return {"ok": True}


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# 1-2: 按钮存在性
# ---------------------------------------------------------------------------


def test_1_normal_assistant_message_has_button(qapp):
    from ui.v2.chat_view import ChatView

    view = ChatView()
    view.append_assistant("你好，我是流萤。", voice_text="你好，我是流萤。")
    buttons = view.findChildren(PlayVoiceButton)
    assert len(buttons) == 1, "普通 assistant 消息必须有播放按钮"
    assert buttons[0].isEnabled()


def test_2_action_paren_message_has_button(qapp):
    from ui.v2.chat_view import ChatView

    view = ChatView()
    view.append_assistant("（听到你轻声呼唤我的名字，我停下手中的动作，转过身来）我在呢。怎么了？",
                          voice_text="（听到你轻声呼唤我的名字，我停下手中的动作，转过身来）我在呢。怎么了？")
    buttons = view.findChildren(PlayVoiceButton)
    assert len(buttons) == 1, "含动作括号的 assistant 消息仍必须有播放按钮"


# ---------------------------------------------------------------------------
# 3: 清洗非空
# ---------------------------------------------------------------------------


def test_3_cleaned_voice_text_nonempty():
    out = remove_action_text("（听到你轻声呼唤我的名字，我停下手中的动作，转过身来）\n我在呢。\n怎么了？")
    assert out == "我在呢。\n怎么了？"
    out = remove_action_text("（听到这个亲昵的称呼，我微微一怔，随后脸颊泛起红晕）\n\n萤宝……？\n\n"
                             "这个称呼，听起来真是让人有些不好意思呢。")
    assert out == "萤宝……？\n这个称呼，听起来真是让人有些不好意思呢。"


# ---------------------------------------------------------------------------
# 4: click → speak 恰一次
# ---------------------------------------------------------------------------


def test_4_click_calls_speak_once(qapp):
    from voice_client.play_button import PlayVoiceButton

    stub = _StubClient()
    btn = PlayVoiceButton(lambda: "（笑）你好，我是流萤。", client=stub)
    btn.click()
    deadline = time.time() + 5
    while time.time() < deadline and btn._op is not None:
        qapp.processEvents()
        time.sleep(0.02)
    assert stub.speak_calls == ["你好，我是流萤。"]  # 清洗后恰一次
    btn.deleteLater()


# ---------------------------------------------------------------------------
# 7: 旧异步结果不得覆盖新状态 (generation guard)
# ---------------------------------------------------------------------------


def test_7_stale_result_ignored(qapp):
    from voice_client.play_button import PlayVoiceButton

    stub = _StubClient()
    btn = PlayVoiceButton(lambda: "你好", client=stub)
    btn.click()                                    # gen=1, speak 在途
    stale_gen = 1
    btn._gen = 2                                   # 模拟 stop 已使代际前移
    btn._op = "speak"
    btn._on_done((stale_gen, {"ok": True}))        # 旧 speak 结果晚到
    assert btn.text() == "⏳ 播放中…", "晚到旧结果被忽略, 按钮保持当前状态(不得被翻成停止)"
    btn.deleteLater()


# ---------------------------------------------------------------------------
# 8: voice.enabled=false → 不发请求
# ---------------------------------------------------------------------------


def test_8_disabled_no_request(tmp_path, monkeypatch):
    cfg_file = tmp_path / "off.yaml"
    cfg_file.write_text("voice:\n  enabled: false\n", encoding="utf-8")
    client = FireflyVoiceClient(load_voice_config(cfg_file))

    def _explode(*a, **k):
        raise AssertionError("disabled 时不得发 HTTP")

    monkeypatch.setattr("urllib.request.urlopen", _explode)
    assert client.speak("你好") == {"ok": False, "reason": "disabled"}


# ---------------------------------------------------------------------------
# 5/6 + 服务器存活: 需真实 Voice Module
# ---------------------------------------------------------------------------


def _server_up() -> bool:
    if os.environ.get("FIREFLY_LIVE_VOICE") != "1":
        return False
    try:
        return httpx.get(f"{BASE}/health", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


FIVE = ("第一句：取消回归验收文本。第二句：此刻应当被停止。"
        "第三句：停止后不允许复活。第四句：生成必须同步取消。第五句：不应被听到。")


@pytest.mark.skipif(not _server_up(), reason="Voice Module 未运行")
class TestServerCancelAndSurvival:
    def test_5_stop_cancels_session(self):
        result = {}

        def speak_async():
            r = httpx.post(f"{BASE}/voice/speak", json={"text": FIVE, "play": True}, timeout=120)
            result.update(r.json())

        th = threading.Thread(target=speak_async)
        th.start()
        time.sleep(1.5)
        t0 = time.perf_counter()
        stop = httpx.post(f"{BASE}/voice/queue/stop", timeout=10)
        stop_ms = (time.perf_counter() - t0) * 1000
        th.join(timeout=120)
        assert stop.status_code == 200 and stop.json().get("cancelled_session")
        assert result.get("cancelled") is True
        assert stop_ms < 200, f"stop 响应 {stop_ms:.0f}ms 超标"
        print(f"\n[stop] {stop_ms:.0f}ms")

    def test_6_replay_after_stop_starts_new_session(self):
        """stop 后再次 play → 新 session 正常启动并播放。"""
        r = httpx.post(f"{BASE}/voice/speak",
                       json={"text": "重新播放验证：这句应当能正常发声。", "play": True}, timeout=120)
        d = r.json()
        assert r.status_code == 200 and d.get("cancelled") is False
        assert d.get("session_id"), "应有新 session"
        deadline = time.time() + 10
        while time.time() < deadline:
            q = httpx.get(f"{BASE}/voice/queue", timeout=5).json()
            if q.get("playing") is None and q.get("queued") == 0:
                break
            time.sleep(0.3)
        time.sleep(0.5)

    def test_server_survives_rapid_stop_cycles(self):
        """v1.3.2 核心回归: 连续 play/stop 压力后服务器必须存活 (PortAudio 竞争修复)。"""
        for i in range(10):
            th = threading.Thread(target=lambda i=i: httpx.post(
                f"{BASE}/voice/speak",
                json={"text": f"存活压力测试第{i}轮，这句话有足够的长度占住播放队列。", "play": True},
                timeout=60))
            th.start()
            time.sleep(0.2 + (i % 3) * 0.15)
            httpx.post(f"{BASE}/voice/queue/stop", timeout=10)
            th.join(timeout=60)
        alive = _server_up()
        assert alive, "连续 play/stop 压力后服务器死亡 (PortAudio 竞争未修复?)"
