# -*- coding: utf-8 -*-
"""firefly-learning — 学习模式插件.

承载学习模式完整实现（原 core/learning）：规划×画像×评估×课程导入，
以及控制台学习回合的编排入口。未安装时控制台学习入口提示安装。
"""
from __future__ import annotations

from core.plugin_api import QuickToolPlugin
from core.quick_tools import QuickToolManifest

PLUGIN_ID = "firefly-learning"
PLUGIN_VERSION = "0.1.0"


class FireflyLearningPlugin(QuickToolPlugin):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.context = None
        self.capability_exposed = False

    @property
    def manifest(self) -> QuickToolManifest:
        return QuickToolManifest(
            id=PLUGIN_ID,
            name="Learning",
            description="学习模式：课程规划/学习者画像/作答评估/复习提醒",
            icon="book",
            version=PLUGIN_VERSION,
            capabilities=("learning",),
            min_api="2",
            author="Firefly",
        )

    def initialize(self, context) -> None:
        self.context = context

    def start(self) -> None:
        self.capability_exposed = True

    def stop(self) -> None:
        self.capability_exposed = False


def create_plugin(parent=None) -> FireflyLearningPlugin:
    return FireflyLearningPlugin(parent)
