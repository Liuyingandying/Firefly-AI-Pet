# -*- coding: utf-8 -*-
"""firefly-vision — 视觉能力插件.

承载屏幕视觉/相机/图片理解的三引擎故障转移实现（原 core/screen_vision）。
宿主的截图提问、屏幕视觉、图片附件、PDF 视觉经能力层调用本插件；
本插件未安装时这些入口提示「暂未安装此插件」。
"""
from __future__ import annotations

from core.plugin_api import QuickToolPlugin
from core.quick_tools import QuickToolManifest

PLUGIN_ID = "firefly-vision"
PLUGIN_VERSION = "0.1.0"


class FireflyVisionPlugin(QuickToolPlugin):
    """视觉能力插件：引擎实现随本包分发，宿主经能力层调用。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.context = None
        self.capability_exposed = False

    @property
    def manifest(self) -> QuickToolManifest:
        return QuickToolManifest(
            id=PLUGIN_ID,
            name="Vision",
            description="视觉能力：屏幕视觉/相机/图片理解（TJU→DeepSeek→GLM 故障转移）",
            icon="eye",
            version=PLUGIN_VERSION,
            capabilities=("vision", "camera"),
            min_api="2",
            author="Firefly",
        )

    def initialize(self, context) -> None:
        self.context = context

    def start(self) -> None:
        self.capability_exposed = True

    def stop(self) -> None:
        self.capability_exposed = False


def create_plugin(parent=None) -> FireflyVisionPlugin:
    return FireflyVisionPlugin(parent)
