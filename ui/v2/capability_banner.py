# -*- coding: utf-8 -*-
"""CapabilityBanner — UI 顶部的插件缺失提示条.

宿主功能入口发现对应插件未安装时，在界面顶部短暂显示
「暂未安装「××」插件，请安装后再使用」，数秒后自动收起。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from ui import theme


class CapabilityBanner(QFrame):
    """顶部提示条：琥珀色软底 + 提示文字，6 秒后自动隐藏。"""

    AUTO_HIDE_MS = 6_000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("capabilityBanner")
        self.setStyleSheet(
            f"#capabilityBanner {{"
            f"  background: rgba(255, 196, 110, 36);"
            f"  border: 1px solid rgba(255, 176, 80, 120);"
            f"  border-radius: {theme.V2.RADIUS_CARD}px;"
            f"}}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        self._label = QLabel("", self)
        self._label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_MAIN}; background: transparent; border: none;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        self._label.setWordWrap(True)
        layout.addWidget(self._label, 1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_missing(self, message: str) -> None:
        """显示一条缺失提示（重复调用刷新文案并重置计时）。"""
        self._label.setText(message)
        self.show()
        self.raise_()
        self._timer.start(self.AUTO_HIDE_MS)
