# -*- coding: utf-8 -*-
"""Emotion Expression Layer (v1.4): emotion/intensity/speaking_style → 表达计划。

不修改 RVC/TTS/AudioQueue 核心, 只产出:
  - 最终语音参数 (speed/pitch/energy/volume → EmotionParams)
  - 句尾标点映射 (comfort: 。→…… / happy: 。→！ …)
  - 句间停顿 (段尾静音填充, ms)

用法:
    plan = resolve_expression("comfort", intensity=0.8, speaking_style="gentle")
    plan.params          # EmotionParams(name="comfort", speed=…, pitch=…)
    plan.pause_ms        # 段尾静音
    plan.punctuate("今天辛苦了。")   # "今天辛苦了……"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from core.emotion import EmotionParams, get_params

MODULE_ROOT = Path(__file__).resolve().parent.parent
EXPRESSION_CONFIG = MODULE_ROOT / "config" / "expression.yaml"

_cache: Optional[dict] = None


def _load() -> dict:
    global _cache
    if _cache is None:
        with open(EXPRESSION_CONFIG, "r", encoding="utf-8") as f:
            _cache = yaml.safe_load(f) or {}
    return _cache


@dataclass
class ExpressionPlan:
    emotion: str
    params: EmotionParams            # 最终语音参数 (模板×intensity×style)
    pause_ms: int                    # 段尾静音填充
    tail_punct: dict                 # 句尾标点映射

    def punctuate(self, sentence: str) -> str:
        """按模板映射句尾终止标点 (只动最后一个终止符, 不改词)。"""
        s = sentence.rstrip()
        if not s:
            return s
        for src, dst in self.tail_punct.items():
            if s.endswith(src):
                return s[: -len(src)] + dst
        return s


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def resolve_expression(
    emotion: str,
    intensity: Optional[float] = None,
    speaking_style: Optional[str] = None,
) -> ExpressionPlan:
    """emotion+intensity+speaking_style → ExpressionPlan。

    - 模板缺失的情绪回退 neutral 参数;
    - intensity None 视为 0.5 (模板原值), 0~1 线性缩放偏移量;
    - speaking_style 未注册时回退 firefly。
    """
    cfg = _load()
    intensity = 0.5 if intensity is None else _clamp(float(intensity), 0.0, 1.0)
    scale_cfg = cfg.get("intensity_scale", {"min": 0.55, "max": 1.45})
    lo, hi = scale_cfg["min"], scale_cfg["max"]
    k = lo + (hi - lo) * intensity          # 0.55 ~ 1.45 缩放系数

    template = cfg.get("templates", {}).get(emotion)

    # 无模板的情绪 (如 v1.3 已有的 sad): 回退 emotion.yaml 原参数, 保持向后兼容
    if not template:
        params = get_params(emotion if emotion in
                            ("neutral", "happy", "excited", "comfort", "sad") else "neutral")
        return ExpressionPlan(emotion=params.name, params=params, pause_ms=120, tail_punct={})

    style = (cfg.get("styles", {}).get(speaking_style)
             or cfg.get("styles", {}).get("firefly") or {})

    speed_t = float(template.get("speed", 1.0))
    pitch_t = float(template.get("pitch", 0))
    energy_t = float(template.get("energy", 1.0))

    # intensity 缩放偏移量; speaking_style 直接乘在最终 speed/energy 上 (可感知)
    speed = (1.0 + (speed_t - 1.0) * k) * float(style.get("speed", 1.0))
    energy = (1.0 + (energy_t - 1.0) * k) * float(style.get("energy", 1.0))
    pitch = pitch_t * k + float(style.get("pitch", 0))

    params = EmotionParams(
        name=emotion,
        speed=_clamp(speed, 0.6, 1.5),
        pitch=int(round(_clamp(pitch, -12, 12))),
        energy=_clamp(energy, 0.4, 1.5),
        volume=str(template.get("volume", "+0%")),
    )
    return ExpressionPlan(
        emotion=params.name,
        params=params,
        pause_ms=int(template.get("pause_ms", 120) * _clamp(k, 0.7, 1.3)),
        tail_punct=dict(template.get("tail_punct", {})),
    )
