# -*- coding: utf-8 -*-
"""firefly-bili-video — B站视频解析插件.

承载 B站视频阅读完整实现（原 core/bili_video_reader + bili_insight_client
+ video_frame_vision + video_reader + video_study + video_time_parser）：
元数据/语音转写/总结/时间点帧问答。未安装时 B站链接入口提示安装。
"""
from __future__ import annotations

from core.plugin_api import QuickToolPlugin
from core.quick_tools import QuickToolManifest

PLUGIN_ID = "firefly-bili-video"
PLUGIN_VERSION = "0.1.0"


class FireflyBiliVideoPlugin(QuickToolPlugin):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.context = None
        self.capability_exposed = False

    @property
    def manifest(self) -> QuickToolManifest:
        return QuickToolManifest(
            id=PLUGIN_ID,
            name="Bili Video",
            description="B站视频解析：元数据/语音转写/AI 总结/时间点帧问答",
            icon="play",
            version=PLUGIN_VERSION,
            capabilities=("bili_video", "video"),
            min_api="2",
            author="Firefly",
        )

    def initialize(self, context) -> None:
        self.context = context

    def start(self) -> None:
        self.capability_exposed = True

    def stop(self) -> None:
        self.capability_exposed = False


def create_plugin(parent=None) -> FireflyBiliVideoPlugin:
    return FireflyBiliVideoPlugin(parent)
