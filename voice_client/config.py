# -*- coding: utf-8 -*-
"""voice_config.yaml 容错加载 (仿 character_loader 的 yaml.safe_load 模式)。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("firefly.voice")

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "voice_config.yaml"

_DEFAULTS: dict[str, Any] = {
    "voice": {
        "enabled": False,
        "auto_play": False,
        "server": {
            "url": "http://127.0.0.1:8300",
            "connect_timeout_s": 2.0,
            "read_timeout_s": 30.0,
        },
        "emotion": {"enabled": True},
        "max_speak_chars": 1600,
    }
}


@dataclass
class VoiceConfig:
    enabled: bool = False
    auto_play: bool = False          # v1.3: False=回复后仅显示播放按钮, 用户点击才朗读
    url: str = "http://127.0.0.1:8300"
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 30.0
    emotion_enabled: bool = True
    max_speak_chars: int = 1600
    source: str = "defaults"
    raw: dict = field(default_factory=dict)


def load_voice_config(path: Path | None = None) -> VoiceConfig:
    """缺文件/坏文件一律回默认值并告警, 绝不让语音配置影响主程序。"""
    cfg_path = Path(path) if path else CONFIG_PATH
    data: dict[str, Any] = {}
    try:
        if cfg_path.is_file():
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
                source = str(cfg_path)
            else:
                log.warning("voice_config.yaml 顶层不是映射, 使用默认配置")
                source = "defaults(bad-yaml)"
        else:
            source = "defaults(missing)"
    except Exception as exc:  # noqa: BLE001 — 配置读取失败不属于致命错误
        log.warning("读取 voice_config.yaml 失败 (%s), 使用默认配置", exc)
        source = "defaults(error)"

    voice = _DEFAULTS["voice"] | (data.get("voice") or {})
    server = _DEFAULTS["voice"]["server"] | (voice.get("server") or {})
    emotion = _DEFAULTS["voice"]["emotion"] | (voice.get("emotion") or {})
    return VoiceConfig(
        enabled=bool(voice.get("enabled", False)),
        auto_play=bool(voice.get("auto_play", False)),
        url=str(server.get("url", _DEFAULTS["voice"]["server"]["url"])).rstrip("/"),
        connect_timeout_s=float(server.get("connect_timeout_s", 2.0)),
        read_timeout_s=float(server.get("read_timeout_s", 30.0)),
        emotion_enabled=bool(emotion.get("enabled", True)),
        max_speak_chars=int(voice.get("max_speak_chars", 600)),
        source=source,
        raw=data,
    )
