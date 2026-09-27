"""Capability gates (read-only adapters over existing plugin state).

两层职责：
- ``video_gate``：视频分析能力的既有开关门。
- 本模块主体：宿主经典功能 ↔ 能力型插件的映射与缺失提示（插件化重构）。
"""

from __future__ import annotations

# 能力标识 → 用户可读名
CAPTION: dict[str, str] = {
    "vision": "视觉",
    "learning": "学习模式",
    "voice_chat": "语音对话",
    "bili_video": "B站视频解析",
    "voice": "语音播报",
}

# 能力标识 → 承载该能力的插件 id（须在 plugin_loader.MANAGED_PLUGIN_IDS 中）
PLUGIN_FOR: dict[str, str] = {
    "vision": "firefly-vision",
    "learning": "firefly-learning",
    "voice_chat": "firefly-voice-chat",
    "bili_video": "firefly-bili-video",
    "voice": "firefly-voice",
}

MISSING_HINT = "请安装后再使用"


def _loaded_ids() -> frozenset[str]:
    from core.plugin_loader import get_loaded_plugin_ids

    return get_loaded_plugin_ids()


def is_available(capability: str) -> bool:
    """该能力对应插件是否已安装加载。未知能力视为可用（非插件化功能）。"""
    plugin_id = PLUGIN_FOR.get(capability)
    if plugin_id is None:
        return True
    return plugin_id in _loaded_ids()


def missing_message(capability: str) -> str:
    """UI 顶部的缺失提示文案。"""
    caption = CAPTION.get(capability, capability)
    return f"暂未安装「{caption}」插件，{MISSING_HINT}"


def require(capability: str) -> None:
    """门控断言：缺插件时抛 :class:`CapabilityMissingError`（携带提示）。"""
    if not is_available(capability):
        raise CapabilityMissingError(capability, missing_message(capability))


class CapabilityMissingError(Exception):
    """能力对应插件未安装。``capability``/``hint`` 供调用方展示提示。"""

    def __init__(self, capability: str, hint: str) -> None:
        super().__init__(hint)
        self.capability = capability
        self.hint = hint


__all__ = [
    "CAPTION", "PLUGIN_FOR", "MISSING_HINT",
    "is_available", "missing_message", "require",
    "CapabilityMissingError",
]
