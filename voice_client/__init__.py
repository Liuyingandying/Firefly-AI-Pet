# -*- coding: utf-8 -*-
"""FireflyVoiceClient: 主工程 → Voice Module 的唯一调用通道。

设计约束 (v1.2):
- 绝不抛异常: 任何失败折返 {"ok": False, "reason": ...}, 聊天功能零影响
- 主工程不感知 TTS/VC 细节, 只给 text/emotion/priority
"""

from voice_client.announcer import VoiceAnnouncer
from voice_client.client import FireflyVoiceClient
from voice_client.config import VoiceConfig, load_voice_config
from voice_client.play_button import PlayVoiceButton
from voice_client.text_cleaner import remove_action_text

__all__ = [
    "FireflyVoiceClient",
    "PlayVoiceButton",
    "VoiceAnnouncer",
    "VoiceConfig",
    "load_voice_config",
    "remove_action_text",
]
