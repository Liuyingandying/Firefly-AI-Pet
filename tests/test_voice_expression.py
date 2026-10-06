# -*- coding: utf-8 -*-
"""v1.4 Emotion Expression Layer 测试: comfort/happy/worried/shy 可区分性。

运行 (需 Voice Module 运行中):
    cd E:\\Firefly_AI_Pet
    .venv\\Scripts\\python.exe -m pytest tests/test_voice_expression.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / ".." / "voice" / "voice_module"))

from voice_client import FireflyVoiceClient  # noqa: E402

BASE = "http://127.0.0.1:8300"
SENTENCE = "今天训练结束得比想象中早，我们一起回去吧。回去的路上，小心一点。"
STYLES = ["comfort", "happy", "worried", "shy"]


def _speak_params(style: str, intensity: float | None = None, speaking_style: str | None = None) -> dict:
    payload = {"text": SENTENCE, "emotion": style, "play": True}
    if intensity is not None:
        payload["intensity"] = intensity
    if speaking_style:
        payload["speaking_style"] = speaking_style
    r = httpx.post(f"{BASE}/voice/speak", json=payload, timeout=120)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def _require_server():
    try:
        assert httpx.get(f"{BASE}/health", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        pytest.skip("Voice Module 未运行")


@pytest.mark.usefixtures("_require_server")
class TestExpressionParams:
    def test_1_four_emotions_all_succeed(self):
        for style in STYLES:
            d = _speak_params(style)
            assert d["segments"], f"{style} 无分段"
            assert d["emotion"] == style

    def test_2_param_vectors_are_distinct(self):
        """四种情绪的 (speed, pitch, energy, pause) 参数向量互不相同 — 可区分的物理基础。"""
        vecs = {}
        for style in STYLES:
            d = _speak_params(style)
            p = d["params"]
            vecs[style] = (p["speed"], p["pitch"], round(p["energy"], 3), d["segments"][0]["text"])
        for i, a in enumerate(STYLES):
            for b in STYLES[i + 1:]:
                assert vecs[a][:3] != vecs[b][:3], f"{a} 与 {b} 参数向量相同, 无法区分"
        assert vecs["happy"][0] > 1.0, "happy 应快于中性语速"
        assert vecs["comfort"][0] < 1.0 and vecs["worried"][0] < 1.0 and vecs["shy"][0] < 1.0
        assert vecs["worried"][2] < vecs["happy"][2], "worried 能量应明显低于 happy"
        print("\n参数向量:", vecs)

    def test_3_tail_punctuation_mapping(self):
        """句尾标点映射: comfort 拖音 / happy 感叹。"""
        d = _speak_params("comfort")
        assert d["segments"][0]["text"].endswith(("……", "……？"))
        d = _speak_params("happy")
        assert d["segments"][-1]["text"].endswith("！")

    def test_4_intensity_scales_expression(self):
        """intensity 0→1 速度单调变化。"""
        low = _speak_params("happy", intensity=0.0)["params"]["speed"]
        high = _speak_params("happy", intensity=1.0)["params"]["speed"]
        assert high > low, f"intensity 未生效: low={low} high={high}"

    def test_5_speaking_style_modifier(self):
        base = _speak_params("comfort")["params"]["speed"]
        gentle = _speak_params("comfort", speaking_style="gentle")["params"]["speed"]
        assert gentle < base, "gentle 风格应更慢"

    def test_6_legacy_sad_unchanged(self):
        """回归: v1.3 已有的 sad 无模板, 走原 emotion.yaml 参数。"""
        d = _speak_params("sad")
        assert d["params"]["speed"] == 0.85 and d["params"]["pitch"] == -2

    def test_7_audition_wavs_saved(self):
        """生成四种情绪试听 wav (同一句话, 控制变量)。"""
        out_dir = Path("E:/Firefly_PageLens/voice/voice_module/examples/emotion_demo")
        out_dir.mkdir(parents=True, exist_ok=True)
        client = FireflyVoiceClient()
        for style in STYLES:
            result = client.speak(SENTENCE, emotion=style, priority=1)
            assert result.get("ok") is True, f"{style} 合成失败: {result}"
            wav = httpx.post(f"{BASE}/voice/speak", json={"text": SENTENCE, "emotion": style},
                             timeout=120)
            (out_dir / f"{style}.wav").write_bytes(wav.content)
        assert all((out_dir / f"{s}.wav").stat().st_size > 50000 for s in STYLES)
