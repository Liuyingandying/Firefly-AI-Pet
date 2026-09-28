# -*- coding: utf-8 -*-
"""输入区的「停止生成」按钮.

发送后被宿主显示（与状态提示同步）；点击发出 stop_requested，
宿主调 runner.stop() 取消当前回合。回复到达/错误/取消时隐藏。
"""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QWidget

from ui import theme


class StopButton(QPushButton):
    """32px 方形停止按钮（深灰底 + 白色方块图标，仿 ZCode 停止键）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(32, 32)
        from PySide6.QtCore import Qt
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("停止生成")
        self.setAccessibleName("停止生成")
        self.setText("■")
        self.setStyleSheet(
            f"QPushButton {{"
            f"  background: rgba(90, 90, 100, 235);"
            f"  color: rgba{theme.V2.ON_PRIMARY_TEXT};"
            f"  border: none; border-radius: {theme.V2.RADIUS_INPUT}px;"
            f"  font-size: 12pt;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(70, 70, 80, 245);"
            f"}}"
        )
        self.hide()
