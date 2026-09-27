# -*- coding: utf-8 -*-
"""LearningStub — learning 插件未安装时控制台的惰性学习控制器.

一切学习入口按「未启用」降级：state.enabled=False、命令/回复返回 None、
其余方法经 __getattr__ 返回无操作调用。接口面与 LearningModeController
对齐（console 实际使用的部分），无需逐处门控。
"""
from __future__ import annotations


class _StubState:
    enabled = False
    active_course_name = ""


class _Noop:
    def __call__(self, *args, **kwargs):
        return None


class LearningStub:
    """惰性学习控制器桩。"""

    entry_pending = False

    def __init__(self) -> None:
        self.state = _StubState()

    def __getattr__(self, name):
        # 未显式实现的方法一律无操作（不会抛错打断聊天）。
        return _Noop()
