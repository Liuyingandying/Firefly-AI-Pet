# -*- coding: utf-8 -*-
"""Voice-1.2 集成测试: FireflyVoiceClient / VoiceAnnouncer / 配置开关。

约定:
- T1/T2 需要真实 Voice Module (127.0.0.1:8300, 见 E:\\Firefly_PageLens\\voice);
  conftest 的外呼封锁仅拦非 loopback, 本测试全部走 127.0.0.1, 合规。
- T3/T4 不依赖语音服务在线。
"""

from __future__ import annotations

import os

import time
from pathlib import Path

import pytest

from core.agent_events import AgentEvent, AgentEventType
from voice_client import FireflyVoiceClient, VoiceAnnouncer
from voice_client.config import load_voice_config

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _final_event(text: str) -> AgentEvent:
    return AgentEvent(
        agent_id="character", type=AgentEventType.FINAL, timestamp=int(time.time()), text=text
    )


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def test_config_defaults_when_missing(tmp_path):
    cfg = load_voice_config(tmp_path / "nope.yaml")
    assert cfg.enabled is False
    assert cfg.url.startswith("http://127.0.0.1")
    assert cfg.emotion_enabled is True


def test_config_disabled_and_garbage(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("- 只有一个列表\n  [", encoding="utf-8")
    assert load_voice_config(bad).enabled is False  # malformed config stays disabled

    off = tmp_path / "off.yaml"
    off.write_text("voice:\n  enabled: false\n", encoding="utf-8")
    assert load_voice_config(off).enabled is False


# ---------------------------------------------------------------------------
# T3: 关闭 voice → 纯文字模式 (不发任何 HTTP)
# ---------------------------------------------------------------------------


def test_t3_disabled_short_circuits(tmp_path, monkeypatch):
    off = tmp_path / "off.yaml"
    off.write_text("voice:\n  enabled: false\n", encoding="utf-8")
    client = FireflyVoiceClient(load_voice_config(off))

    def _explode(*args, **kwargs):  # 若发起 HTTP 则测试失败
        raise AssertionError("disabled 模式不应发起 HTTP")

    monkeypatch.setattr("urllib.request.urlopen", _explode)
    result = client.speak("你好，流萤")
    assert result == {"ok": False, "reason": "disabled"}


# ---------------------------------------------------------------------------
# T4: Voice 服务未启动 → 快速失败, 不抛异常, 不阻塞
# ---------------------------------------------------------------------------


def test_t4_server_down_fails_fast(tmp_path):
    cfg = load_voice_config(tmp_path / "none.yaml")
    cfg.enabled = True  # Exercise configured external-service failure, not disabled defaults.
    cfg.url = "http://127.0.0.1:59997"  # 未监听端口
    client = FireflyVoiceClient(cfg)

    t0 = time.perf_counter()
    result = client.speak("你好，流萤")
    elapsed = time.perf_counter() - t0
    assert result["ok"] is False
    assert result["reason"] in {"unavailable", "timeout", "error"}
    assert elapsed < 10, f"服务不可用时应快速折返, 实测 {elapsed:.1f}s"


def test_t4_announcer_never_raises_on_dead_server(tmp_path):
    cfg = load_voice_config(tmp_path / "none.yaml")
    cfg.url = "http://127.0.0.1:59997"
    announcer = VoiceAnnouncer(FireflyVoiceClient(cfg))
    announcer.on_agent_event(_final_event("这句话会静默失败"))  # 不应抛出
    announcer.on_agent_event(
        AgentEvent(agent_id="character", type=AgentEventType.CANCELLED, timestamp=0)
    )
    deadline = time.time() + 10
    while announcer._spinning and time.time() < deadline:
        time.sleep(0.05)
    assert not announcer._spinning


# ---------------------------------------------------------------------------
# T1/T2: 真实 Voice Module 在线链路 (127.0.0.1:8300)
# ---------------------------------------------------------------------------


class TestLiveVoiceModule:
    @pytest.fixture(autouse=True)
    def _require_server(self):
        import urllib.request

        if os.environ.get("FIREFLY_LIVE_VOICE") != "1":
            pytest.skip("Live voice is opt-in: FIREFLY_LIVE_VOICE=1")
        try:
            with urllib.request.urlopen("http://127.0.0.1:8300/health", timeout=2) as resp:
                if resp.status != 200:
                    pytest.skip("Voice Module 未运行 (http 非200)")
        except Exception:  # noqa: BLE001
            pytest.skip("Voice Module 未运行 (127.0.0.1:8300)")

    def test_t1_hello_firefly(self):
        """用户: 你好，流萤 → 情绪自动推断 + 真实播放。"""
        client = FireflyVoiceClient()
        result = client.speak("你好，流萤")
        assert result["ok"] is True, result
        assert result["segments"] >= 1
        assert result["emotion"] in {"neutral", "happy", "comfort", "excited", "sad"}

    def test_t2_long_text_multisegment(self):
        """长文本 → Voice Module 自动分句, 多段播放。"""
        client = FireflyVoiceClient()
        text = (
            "平面波是波面为平面的波。它的振幅在与传播方向垂直的平面上处处相同。"
            "例如远处的声波和均匀光束都可以近似看作平面波。明天我们再看球面波，记得复习哦。"
        )
        result = client.speak(text)
        assert result["ok"] is True, result
        assert result["segments"] >= 3, f"长文本应分句, 实际 {result['segments']} 段"
