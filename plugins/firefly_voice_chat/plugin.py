# -*- coding: utf-8 -*-
"""firefly-voice-chat — 语音对话插件.

承载语音对话完整实现（原 voice_client + ui/v2/voice_orb/voice_session）：
麦克风采集→STT→对话→TTS+RVC 朗读→打断/静音/状态圆球。
未安装时「语音」入口与回复播放按钮提示安装。
"""
from __future__ import annotations

# 本插件目录进 sys.path：宿主/其他插件经它导入 voice_client 与 voice_ui。
import sys as _sys
from pathlib import Path as _Path
_SELF = _Path(__file__).resolve().parent
if str(_SELF) not in _sys.path:
    _sys.path.insert(0, str(_SELF))

from core.plugin_api import QuickToolPlugin
from core.quick_tools import QuickToolManifest

PLUGIN_ID = "firefly-voice-chat"
PLUGIN_VERSION = "0.1.0"


class FireflyVoiceChatPlugin(QuickToolPlugin):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.context = None
        self.capability_exposed = False

    @property
    def manifest(self) -> QuickToolManifest:
        return QuickToolManifest(
            id=PLUGIN_ID,
            name="Voice Chat",
            description="语音对话：麦克风 STT + 回复自动朗读 + 打断/静音/状态圆球",
            icon="mic",
            version=PLUGIN_VERSION,
            capabilities=("voice_chat", "voice", "audio"),
            min_api="2",
            author="Firefly",
        )

    def initialize(self, context) -> None:
        self.context = context

    def start(self) -> None:
        self.capability_exposed = True

    def stop(self) -> None:
        self.capability_exposed = False


def create_plugin(parent=None) -> FireflyVoiceChatPlugin:
    return FireflyVoiceChatPlugin(parent)
