# -*- coding: utf-8 -*-
"""Emotion Layer: 解析情绪 → 查表取语音参数。

两级解析:
1. Agent 显式传入 emotion (推荐, LLM 输出 {"text","emotion"})
2. 未传时基于中文关键词规则自动推断 (个人 Demo 级启发式, 可后续换小型分类模型)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import yaml

MODULE_ROOT = Path(__file__).resolve().parent.parent
EMOTION_CONFIG = MODULE_ROOT / "config" / "emotion.yaml"

EMOTIONS = ("neutral", "happy", "excited", "comfort", "sad")

# 关键词规则按顺序匹配 (comfort/sad 优先级高, 避免被通用积极词抢先)
RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("comfort", ("辛苦", "休息", "别担心", "不用担心", "没事的", "慢慢来", "抱抱", "不着急", "会好的", "晚安", "早点睡")),
    ("sad", ("对不起", "抱歉", "遗憾", "难过", "可惜", "失败了", "考砸", "呜呜", "失落")),
    ("excited", ("太厉害", "无敌", "燃起来了", "冲鸭", "冲啊", "哇塞", "惊呆了", "爆了", "第一名", "夺冠")),
    ("happy", ("哈哈", "开心", "太好了", "太棒", "高兴", "真棒", "好耶", "成功啦", "通过啦", "恭喜", "🎉", "😄", "~~~~")),
)


@dataclass(frozen=True)
class EmotionParams:
    name: str
    speed: float = 1.0      # TTS 语速倍率
    pitch: int = 0          # RVC 变调半音
    energy: float = 1.0     # 输出能量增益
    volume: str = "+0%"     # edge-tts 音量

    @property
    def tts_rate(self) -> str:
        """edge-tts rate 参数: 语速 1.1 → '+10%'。"""
        pct = int(round((self.speed - 1.0) * 100))
        return f"{'+' if pct >= 0 else ''}{pct}%"


_params_cache: Optional[dict] = None
_lock = threading.Lock()


def _load_table() -> dict:
    global _params_cache
    with _lock:
        if _params_cache is None:
            with open(EMOTION_CONFIG, "r", encoding="utf-8") as f:
                _params_cache = yaml.safe_load(f) or {}
            # 未定义的情绪回退 neutral
            for name in EMOTIONS:
                _params_cache.setdefault(name, {})
        return _params_cache


def get_params(emotion: str) -> EmotionParams:
    """查表; 未知情绪安全回退 neutral。"""
    table = _load_table()
    raw = table.get(emotion) or table.get("neutral") or {}
    return EmotionParams(
        name=emotion if emotion in EMOTIONS else "neutral",
        speed=float(raw.get("speed", 1.0)),
        pitch=int(raw.get("pitch", 0)),
        energy=float(raw.get("energy", 1.0)),
        volume=str(raw.get("volume", "+0%")),
    )


def parse_emotion(text: str, explicit: Optional[str] = None) -> Tuple[str, str]:
    """返回 (emotion, source)。source: explicit | keyword | default"""
    if explicit:
        name = explicit.lower().strip()
        if name in EMOTIONS:
            return name, "explicit"
        # Agent 传了未注册情绪 → 不报错, 回退并提示来源
        return "neutral", f"unknown:{explicit}"
    for emotion, words in RULES:
        if any(w in text for w in words):
            return emotion, "keyword"
    return "neutral", "default"
