# -*- coding: utf-8 -*-
"""端到端自动化测试: 输入音频 → convert_voice → 输出存在且时长合理。

运行:
    cd voice_module
    python -m tests.test_pipeline          # 直接跑
    pytest tests/test_pipeline.py -v      # pytest 模式
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_ROOT))

import soundfile as sf  # noqa: E402

INPUT = MODULE_ROOT / "examples" / "input.wav"
OUTPUT = MODULE_ROOT / "output" / "test_pipeline_result.wav"


class TestVoicePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert INPUT.is_file(), f"缺少测试输入: {INPUT}"
        from core.pipeline import convert_voice

        cls.convert_voice = convert_voice
        cls.result = convert_voice(str(INPUT), speaker_model="firefly", output_audio=str(OUTPUT))

    def test_output_exists(self):
        self.assertTrue(OUTPUT.is_file(), "输出文件未生成")

    def test_output_writable_duration(self):
        data, sr = sf.read(str(OUTPUT))
        info = sf.info(str(INPUT))
        in_duration = info.frames / info.samplerate
        out_duration = len(data) / sr
        self.assertGreater(out_duration, in_duration * 0.8, f"输出时长异常: {out_duration:.2f}s vs 输入 {in_duration:.2f}s")
        self.assertLess(out_duration, in_duration * 1.5, "输出时长异常膨胀")

    def test_audio_not_silent(self):
        self.assertGreater(abs(self.result.audio).max(), 1e-3, "输出为静音")

    def test_sample_rate(self):
        self.assertGreaterEqual(self.result.sample_rate, 16000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
