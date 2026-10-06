# -*- coding: utf-8 -*-
"""v1.3.1 Bugfix 测试: 星号/Markdown 清洗 (Test1-4) + 播放取消 (Test5-7)。

Test1-4 纯离线; Test5-7 需要真实 Voice Module 服务 (127.0.0.1:8300, 无则跳过)。
"""

from __future__ import annotations

import os

import threading
import time
from pathlib import Path

import httpx
import pytest

from voice_client import remove_action_text

PROJECT_DIR = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8300"

FIVE_SENTENCES = (
    "第一句：这是取消机制的验收文本。第二句：播放到这句时应当被停止。"
    "第三句：停止之后不允许再次响起。第四句：后台生成必须同步取消。"
    "第五句：这句永远不应该被听到。"
)


# ---------------------------------------------------------------------------
# BUG2: Markdown/星号清洗
# ---------------------------------------------------------------------------


class TestCleanerV131:
    def test_1_bold_action_before_dialogue(self):
        """Test1: **（动作）** + 对白 → 只剩对白, 无星号无括号。"""
        out = remove_action_text("**（我轻轻笑了一下）**\n\n你好呀。")
        assert out == "你好呀。"
        for ch in ("*", "（", "）"):
            assert ch not in out

    def test_2_action_line_and_bold_dialogue(self):
        """Test2: （动作）+ **对白** → 只读对白。"""
        out = remove_action_text("（我低下头）\n\n**今天辛苦了。**")
        assert out == "今天辛苦了。"
        assert "*" not in out

    def test_3_inline_bold_content_kept(self):
        """Test3: 行内加粗正文保留、星号去除。"""
        assert remove_action_text("这个参数非常**重要**。") == "这个参数非常重要。"

    def test_4_math_and_terms_protected(self):
        """Test4: f(x)=x² 保留; 乘号读作"乘"; 全角括号术语转停顿; ~~/#/- 清除。"""
        assert remove_action_text("f(x)=x²") == "f(x)=x²"
        assert remove_action_text("2 * 3 = 6") == "2 乘 3 = 6"
        out = remove_action_text("RVC（Retrieval-based Voice Conversion）很强。")
        assert "Retrieval-based Voice Conversion" in out and "（" not in out
        out = remove_action_text("~~过期信息~~请忽略。\n\n# 标题\n\n- 列表项内容")
        assert "~~" not in out and "#" not in out
        assert "过期信息请忽略。" in out and "标题" in out and "列表项内容" in out
        # 单星斜体动作行 (无括号) 整行删除
        assert remove_action_text("*她轻轻叹了口气。*") == ""
        assert remove_action_text("**（沉默）**你好。") == "你好。"
        # 下划线强调
        assert remove_action_text("这很__关键__。") == "这很关键。"

    def test_display_vs_voice_split(self):
        """display_text != voice_text 的核心样例。"""
        display = "（我轻轻笑了一下）\n\n笨蛋，今天也辛苦啦。"
        assert remove_action_text(display) == "笨蛋，今天也辛苦啦。"


# ---------------------------------------------------------------------------
# BUG1: 播放取消 (服务器级, 需真实服务)
# ---------------------------------------------------------------------------


def _server_up() -> bool:
    if os.environ.get("FIREFLY_LIVE_VOICE") != "1":
        return False
    try:
        return httpx.get(f"{BASE}/health", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _post(path: str, **kwargs):
    return httpx.post(f"{BASE}{path}", timeout=120, **kwargs)


class TestCancellationLive:
    @pytest.fixture(autouse=True)
    def _require_server(self):
        if not _server_up():
            pytest.skip("Voice Module 未运行 (127.0.0.1:8300)")

    def test_5_stop_mid_playback_no_revival(self):
        """Test5: 5句播放, 第1句期间 stop → 立即静音, 剩余4句不得播放。"""
        result = {}

        def speak_async():
            r = _post("/voice/speak", json={"text": FIVE_SENTENCES, "play": True, "priority": 1})
            result["segments"] = len(r.json().get("segments", []))
            result["cancelled"] = r.json().get("cancelled")

        th = threading.Thread(target=speak_async)
        th.start()
        time.sleep(1.5)                       # 第1句合成/播放中
        t0 = time.perf_counter()
        stop = _post("/voice/queue/stop")
        stop_ms = (time.perf_counter() - t0) * 1000
        th.join(timeout=120)

        assert stop.status_code == 200
        assert stop.json().get("cancelled_session"), "stop 应取消活跃会话"
        print(f"\n[stop 响应耗时] {stop_ms:.0f} ms | speak segments={result.get('segments')} "
              f"cancelled={result.get('cancelled')}")
        assert result.get("cancelled") is True, "后台合成应被取消"
        assert (result.get("segments") or 0) <= 4, "取消后不应合成全部5句"

        # 观察 12s: 不允许任何新播放段出现
        time.sleep(0.6)
        for _ in range(24):
            q = httpx.get(f"{BASE}/voice/queue", timeout=5).json()
            assert q.get("playing") is None, f"stop 后声音复活: {q}"
            time.sleep(0.5)

    def test_6_stale_session_dropped_next_session_plays(self):
        """Test6: 停止 A 后立即播放 B → 只听到 B, A 晚到结果被丢弃。"""
        text_a = "甲会话第一句，用来占住播放队列。甲会话第二句。甲会话第三句。"
        result_a = {}

        def speak_a():
            r = _post("/voice/speak", json={"text": text_a, "play": True, "priority": 1})
            result_a.update(r.json())

        th = threading.Thread(target=speak_a)
        th.start()
        time.sleep(1.2)
        _post("/voice/queue/stop")            # 停 A
        r_b = _post("/voice/speak", json={"text": "乙会话的唯一一句话。", "play": True, "priority": 1})
        th.join(timeout=120)

        assert r_b.status_code == 200
        segs_b = r_b.json().get("segments", [])
        assert segs_b and "乙会话" in segs_b[0]["text"]
        # A 的返回必须是取消态, 且其段文本不得进入 B 之后队列
        assert result_a.get("cancelled") is True
        for seg in result_a.get("segments", []):
            assert "甲会话" not in (seg.get("text") or "") or result_a.get("cancelled")
        time.sleep(1.0)
        q = httpx.get(f"{BASE}/voice/queue", timeout=5).json()
        playing_text = (q.get("playing") or {}).get("text", "")
        assert "甲会话" not in playing_text

    def test_7_play_stop_cycles_no_leak(self):
        """Test7: play/stop 交替 5 次 — 无死锁/异常/旧音频复活。"""
        import threading as _t

        threads_before = _t.active_count()
        for i in range(5):
            th = _t.Thread(
                target=lambda i=i: _post("/voice/speak", json={
                    "text": f"第{i}轮的取消循环测试句子。",
                    "play": True, "priority": 1,
                })
            )
            th.start()
            time.sleep(0.3 + i * 0.1)         # 有的在合成期停, 有的在播放期停
            stop = _post("/voice/queue/stop")
            assert stop.status_code == 200
            th.join(timeout=60)
            assert not th.is_alive(), f"第{i}轮 speak 线程卡死"
        deadline = time.time() + 15
        while time.time() < deadline:
            q = httpx.get(f"{BASE}/voice/queue", timeout=5).json()
            if q.get("playing") is None and q.get("queued") == 0:
                break
            time.sleep(0.5)
        q = httpx.get(f"{BASE}/voice/queue", timeout=5).json()
        assert q.get("playing") is None and q.get("queued") == 0
        threads_after = _t.active_count()
        print(f"\n[线程数] before={threads_before} after={threads_after}")
        assert threads_after <= threads_before + 3
