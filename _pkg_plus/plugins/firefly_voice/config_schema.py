# -*- coding: utf-8 -*-
"""firefly_voice 插件的配置 schema 与读写（面向 UI/状态展示的薄层）。

解析（读）统一走 ``voice_client.config.load_voice_config`` 的管理链：
    环境变量 > 用户插件配置 > 旧配置文件 > 默认值
写入统一走 ``voice_client.config.save_managed_overrides``（原子、哑失败）。
本模块只补充：键名常量、默认值快照、以及面向插件的读取辅助。
"""

from __future__ import annotations

from typing import Any

from voice_client.config import (
    ENV_AUTO_PLAY,
    ENV_ENABLED,
    ENV_URL,
    USER_CONFIG_NAME,
    USER_PLUGIN_DIR_NAME,
    VoiceConfig,
    load_voice_config,
    managed_user_config_path,
    save_managed_overrides,
)

#: 插件默认值（与 voice_client.config._MANAGED_DEFAULTS 对齐，供 UI 展示）
DEFAULTS: dict[str, Any] = {
    "enabled": True,            # 语音能力默认存在
    "auto_play": False,         # v1.3：默认仅播放按钮
    "url": "http://127.0.0.1:8300",
    "max_speak_chars": 1600,
    "service_script": "api/server.py",
}

__all__ = [
    "DEFAULTS",
    "ENV_AUTO_PLAY",
    "ENV_ENABLED",
    "ENV_URL",
    "USER_CONFIG_NAME",
    "USER_PLUGIN_DIR_NAME",
    "VoiceConfig",
    "config_path",
    "load_voice_config",
    "save_managed_overrides",
    "set_auto_play",
    "set_enabled",
    "set_service_paths",
]


def config_path():
    """用户插件配置的绝对路径（用于 UI 提示与诊断）。"""
    return managed_user_config_path()


def set_enabled(enabled: bool) -> bool:
    """开关语音能力（写入用户插件配置；UI 勾选与插件 API 共用）。"""
    return save_managed_overrides({"enabled": bool(enabled)})


def set_auto_play(auto_play: bool) -> bool:
    """设置回复完成自动朗读（v1.3 默认 False=仅按钮）。"""
    return save_managed_overrides({"auto_play": bool(auto_play)})


def set_service_paths(*, root: str | None = None, python: str | None = None,
                      script: str | None = None) -> bool:
    """配置外部服务路径（机器相关值只落用户数据）。"""
    updates: dict[str, Any] = {}
    if root is not None:
        updates["service_root"] = root
    if python is not None:
        updates["service_python"] = python
    if script is not None:
        updates["service_script"] = script
    return save_managed_overrides(updates)
