# -*- coding: utf-8 -*-
"""v1.1 表达层测试: 情绪解析 / 分句 / speak 管道 / 长文本 / 连续与中断。

运行 (进程内, 不依赖 HTTP 服务, 但需要网络做 edge-tts):
    cd voice_module
    python -m tests.test_v11          # 或 pytest tests/test_v11.py -v

注意: 涉及真实 edge-tts 网络合成 + RVC GPU 推理, 全套约 1-3 分钟。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_ROOT))

import numpy as np  # noqa: E402

from core.emotion import get_params, parse_emotion  # noqa: E402
from core.speech import apply_energy, speak_text  # noqa: E402
from core.splitter import split_sentences  # noqa: E402


class TestEmotionLayer(unittest.TestCase):
    def test_explicit_emotion(self):
        self.assertEqual(parse_emotion("随便什么", explicit="sad"), ("sad", "explicit"))
        self.assertEqual(parse_emotion("随便什么", explicit="HAPPY"), ("happy", "explicit"))

    def test_unknown_fallback(self):
        emotion, source = parse_emotion("x", explicit="angry")
        self.assertEqual(emotion, "neutral")
        self.assertTrue(source.startswith("unknown"))

    def test_keyword_comfort(self):
        emotion, source = parse_emotion("今天也辛苦了，要早点休息哦")
        self.assertEqual(emotion, "comfort")
        self.assertEqual(source, "keyword")

    def test_keyword_happy_and_sad(self):
        self.assertEqual(parse_emotion("哈哈，太好了，我们成功啦！")[0], "happy")
        self.assertEqual(parse_emotion("对不起，这次没考好，有点难过")[0], "sad")

    def test_default_neutral(self):
        self.assertEqual(parse_emotion("现在气温二十二度。")[0], "neutral")

    def test_params_table(self):
        p = get_params("comfort")
        self.assertEqual((p.speed, p.pitch), (0.95, 1))
        self.assertEqual(p.tts_rate, "-5%")
        self.assertEqual(get_params("happy").tts_rate, "+10%")
        self.assertEqual(get_params("不存在的情绪").name, "neutral")


class TestSplitter(unittest.TestCase):
    def test_basic_split(self):
        s = split_sentences("今天也要努力学习哦。明天继续加油！")
        self.assertEqual(len(s), 2)

    def test_long_clause_split_and_merge(self):
        long_text = "我们要注意的是" + "一个非常长" * 12 + "的问题，然后继续分析它，最后总结结论。"
        s = split_sentences(long_text)
        self.assertGreaterEqual(len(s), 2)
        self.assertTrue(all(len(x) <= 80 for x in s))

    def test_fragment_merge(self):
        s = split_sentences("好。今天我们讲一个非常非常重要的话题，请大家认真听。记住了吗？")
        self.assertTrue(all(len(x) >= 4 for x in s))


class TestSpeakPipeline(unittest.TestCase):
    """真实链路: edge-tts + RVC (需要网络与 GPU)。"""

    @classmethod
    def setUpClass(cls):
        cls.result_normal = speak_text("今天也要努力学习哦。", emotion="neutral")
        cls.result_comfort = speak_text("今天也辛苦了，要早点休息哦。", emotion="comfort")

    def test_01_normal_text_single_segment(self):
        r = self.result_normal
        self.assertEqual(r.emotion, "neutral")
        self.assertGreaterEqual(len(r.segments), 1)
        wav = r.to_wav_bytes()
        self.assertGreater(len(wav), 10000, "wav 过小")
        self.assertGreater(r.sample_rate, 16000)

    def test_02_emotion_params_applied(self):
        r = self.result_comfort
        self.assertEqual(r.emotion, "comfort")
        self.assertEqual(r.segments[0].emotion, "comfort")
        # comfort 语速 0.95 vs neutral 1.0 → 时长差异在合理带宽内 (网络 TTS 有抖动)
        d_c = self.result_comfort.segments[0].duration_s
        d_n = self.result_normal.segments[0].duration_s
        self.assertGreater(d_c, 0)
        self.assertGreater(d_n, 0)
        self.assertTrue(0.5 < d_c / d_n < 2.0, f"时长比异常: {d_c}/{d_n}")

    def test_03_energy_gain_and_limit(self):
        base = np.ones(1000, dtype=np.float32) * 0.5
        boosted = apply_energy(base, 1.2)          # 0.6, 不削顶
        self.assertAlmostEqual(float(np.abs(boosted).max()), 0.6, places=2)
        clipped = apply_energy(np.ones(1000, dtype=np.float32), 1.2)  # 1.2 → 限幅 0.98
        self.assertLessEqual(float(np.abs(clipped).max()), 0.981)

    def test_04_long_text_multisegment(self):
        text = ("今天我们学习了三个新知识点。第一个是语音情绪的表达方式！"
                "第二个是长文本的分句处理。第三个是播放队列的优先级机制。"
                "明天我们继续深入，记得复习哦。")
        r = speak_text(text, emotion="happy")
        self.assertGreaterEqual(len(r.segments), 4, "长文本应切分为多句")
        durs = sum(s.duration_s for s in r.segments)
        full = len(r.concat_audio()) / r.sample_rate
        self.assertTrue(abs(durs - full) < 1.0, "拼接时长应约等于分段之和")
        self.assertGreater(len(r.to_wav_bytes()), 100000)


class TestAudioQueue(unittest.TestCase):
    """真实播放 (有设备走 sounddevice, 无设备自动模拟)。"""

    def _mk_queue(self):
        from audio.audio_queue import AudioItem, AudioQueue

        events = []
        q = AudioQueue(on_event=lambda kind, item: events.append((kind, item.text if item else None)))
        return q, events

    @staticmethod
    def _item(text: str, seconds: float, priority: int = 1) -> "AudioItem":
        from audio.audio_queue import AudioItem

        return AudioItem(audio=np.zeros(int(24000 * seconds), dtype=np.float32),
                         sample_rate=24000, text=text, priority=priority)

    def test_05_priority_order_and_drain(self):
        q, events = self._mk_queue()
        q.pause()                       # 确定性验证堆排序: 两项都在入队后才开取
        q.enqueue(self._item("低先", 0.2, 1))
        q.enqueue(self._item("高先", 0.2, 5))
        self.assertEqual(q.status()["queued"], 2)
        q.resume()
        self.assertTrue(q.wait_idle(timeout=15))
        texts = [t for k, t in events if k == "start"]
        self.assertEqual(texts, ["高先", "低先"], "高优先级应先播")
        self.assertIn("finish", [k for k, _ in events])

    def test_06_interrupt_by_urgent(self):
        q, events = self._mk_queue()
        q.enqueue(self._item("很长的一句话正在慢慢播放" , 4.0, 1))
        time.sleep(0.4)                      # 确保长句已开始播放
        q.enqueue(self._item("紧急提醒", 0.3, 10))   # 抢占
        self.assertTrue(q.wait_idle(timeout=15))
        texts = [t for k, t in events if k == "start"]
        self.assertEqual(texts[0], "很长的一句话正在慢慢播放")
        self.assertIn("紧急提醒", texts)
        self.assertIn("interrupted", [k for k, _ in events], "长句应被打断")
        # 紧急项完成后, 原 4s 长句不应继续播完 (总耗时显著小于 4.3s)
        kinds = [k for k, _ in events]
        self.assertEqual(kinds.count("finish"), 1)

    def test_07_continuous_enqueue_fifo(self):
        q, events = self._mk_queue()
        for i in range(5):
            q.enqueue(self._item(f"句子{i}", 0.15, 1))
        self.assertTrue(q.wait_idle(timeout=20))
        texts = [t for k, t in events if k == "start"]
        self.assertEqual(texts, [f"句子{i}" for i in range(5)], "同优先级应 FIFO")


if __name__ == "__main__":
    unittest.main(verbosity=2)
