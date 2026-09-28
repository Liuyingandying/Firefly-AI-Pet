# -*- coding: utf-8 -*-
"""v1.3.1 取消机制单元测试 (voice module 侧, 不依赖网络 TTS)。

运行:
    cd <voice_module>
    python -m tests.test_cancellation
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

MODULE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_ROOT))

from core.speech import (  # noqa: E402
    Segment,
    SpeechSession,
    _CancelledError,
    _synthesize_segment,
    speak_text,
)
from core.splitter import split_sentences  # noqa: E402

FIVE = "第一句测试。第二句测试。第三句测试。第四句测试。第五句测试。"


class FakeSynth:
    """替换 _synthesize_segment: 即时假段, 可在指定调用序号时取消会话。"""

    def __init__(self, cancel_on_call: int | None = None, session: SpeechSession | None = None):
        self.calls = 0
        self.cancel_on_call = cancel_on_call
        self.session = session
        self.lock = threading.Lock()

    def __call__(self, text, params, speaker_model, pitch, session=None):
        with self.lock:
            self.calls += 1
            n = self.calls
        if self.cancel_on_call is not None and n >= self.cancel_on_call and self.session:
            self.session.cancel()          # 模拟"用户在合成第N句时点了停止"
        time.sleep(0.01)
        if session is not None and session.cancelled:
            raise _CancelledError()
        return Segment(text=text, emotion=params.name,
                       audio=np.zeros(240, dtype=np.float32), sample_rate=24000)


class TestSpeechCancellation(unittest.TestCase):
    def test_cancel_mid_generation_stops_pipeline(self):
        """第2句合成时取消 → 只完成第1句, cancelled=True, 不再生成第3~5句。"""
        session = SpeechSession()
        fake = FakeSynth(cancel_on_call=2, session=session)
        original = None
        import core.speech as speech_mod

        original = speech_mod._synthesize_segment
        speech_mod._synthesize_segment = fake
        try:
            result = speak_text(FIVE, emotion="neutral", play=False, session=session)
        finally:
            speech_mod._synthesize_segment = original
        self.assertTrue(result.cancelled)
        self.assertLessEqual(len(result.segments), 2)
        self.assertEqual(fake.calls, 2)          # 第3句不再进入合成

    def test_no_cancel_all_segments(self):
        session = SpeechSession()
        fake = FakeSynth()
        import core.speech as speech_mod

        original = speech_mod._synthesize_segment
        speech_mod._synthesize_segment = fake
        try:
            result = speak_text(FIVE, emotion="neutral", play=False, session=session)
        finally:
            speech_mod._synthesize_segment = original
        self.assertFalse(result.cancelled)
        self.assertEqual(len(result.segments), 5)


class TestQueueSessionDrop(unittest.TestCase):
    def test_cancelled_session_late_items_dropped(self):
        """BUG1 并发保护: 会话A播放中取消 → 当前被打断, 晚到段丢弃, B 正常播放。"""
        from audio.audio_queue import AudioItem, AudioQueue

        events = []
        q = AudioQueue(on_event=lambda k, i: events.append((k, (i.text if i else ""), (i.session_id if i else None))))
        try:
            q.enqueue(AudioItem(audio=np.zeros(24000, dtype=np.float32), sample_rate=24000,
                                text="A1", session_id="A"))      # 1s 音频
            time.sleep(0.15)                 # 确保 A1 已开播
            q.cancel_session("A")            # 用户点停止: 打断 A1 + 清 A 队列
            q.enqueue(AudioItem(audio=np.zeros(120, dtype=np.float32), sample_rate=24000,
                                text="A2-late", session_id="A"))   # 晚到结果
            self.assertTrue(q.wait_idle(timeout=10))
            self.assertEqual(q.status()["queued"], 0)
            starts = [t for k, t, _ in events if k == "start"]
            self.assertEqual(starts, ["A1"])
            self.assertIn("interrupted", [k for k, _, _ in events])   # A1 被打断
            dropped = [t for k, t, _ in events if k == "dropped"]
            self.assertIn("A2-late", dropped)

            # 新会话 B 不受影响
            q.enqueue(AudioItem(audio=np.zeros(120, dtype=np.float32), sample_rate=24000,
                                text="B1", session_id="B"))
            self.assertTrue(q.wait_idle(timeout=10))
            starts = [t for k, t, _ in events if k == "start"]
            self.assertIn("B1", starts)
        finally:
            q.clear()
            q.stop_current()

    def test_cancel_latency(self):
        """停止响应: cancel_session (停当前+清队列) < 200ms。"""
        from audio.audio_queue import AudioItem, AudioQueue

        q = AudioQueue()
        try:
            for i in range(50):              # 预置较深队列
                q.enqueue(AudioItem(audio=np.zeros(24000 * 3, dtype=np.float32),
                                    sample_rate=24000, text=f"s{i}", session_id="X"))
            time.sleep(0.2)                  # 让第一段进入播放
            t0 = time.perf_counter()
            q.cancel_session("X")
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self.assertLess(elapsed_ms, 200, f"取消耗时 {elapsed_ms:.0f}ms 超标")
            self.assertEqual(q.status()["queued"], 0)
        finally:
            q.stop_current()
            q.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
