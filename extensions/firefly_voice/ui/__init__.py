# -*- coding: utf-8 -*-
"""Voice Settings 对话框（经插件系统暴露；不改聊天界面）。

内容：
[✓] Voice Enabled   [ ] Auto Play
Voice Service: ONLINE/OFFLINE (url, latency)
按钮：启动语音服务 / 停止语音服务 / 测试播放 / 刷新 / 关闭
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from firefly_voice.config_schema import config_path, load_voice_config


class VoiceSettingsDialog(QDialog):
    """Modeless voice capability panel owned by the plugin."""

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self.setWindowTitle("Voice Settings")
        self.setModal(False)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        self._enabled = QCheckBox("Voice Enabled")
        self._enabled.toggled.connect(self._on_enabled_toggled)
        layout.addWidget(self._enabled)

        self._auto_play = QCheckBox("Auto Play (reply finished → speak immediately)")
        self._auto_play.toggled.connect(self._on_auto_play_toggled)
        layout.addWidget(self._auto_play)

        self._service = QLabel("Voice Service: —")
        self._service.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._service)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet("color: #888; font-size: 8pt;")
        layout.addWidget(self._detail)

        buttons = QHBoxLayout()
        self._start_btn = QPushButton("启动语音服务")
        self._start_btn.clicked.connect(self._on_start)
        buttons.addWidget(self._start_btn)

        self._stop_btn = QPushButton("停止语音服务")
        self._stop_btn.clicked.connect(self._on_stop)
        buttons.addWidget(self._stop_btn)

        self._test_btn = QPushButton("测试播放")
        self._test_btn.clicked.connect(self._on_test)
        buttons.addWidget(self._test_btn)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        buttons.addWidget(refresh_btn)

        layout.addLayout(buttons)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: #888; font-size: 8pt;")
        layout.addWidget(self._hint)

        self.refresh()

    # -- 状态刷新 ---------------------------------------------------------------

    def refresh(self) -> None:
        cfg = load_voice_config()
        self._enabled.blockSignals(True)
        self._auto_play.blockSignals(True)
        self._enabled.setChecked(bool(cfg.enabled))
        self._auto_play.setChecked(bool(cfg.auto_play))
        self._enabled.blockSignals(False)
        self._auto_play.blockSignals(False)

        health = self._plugin.health_check()
        state = "ONLINE" if health.get("online") else "OFFLINE"
        latency = health.get("latency_ms")
        self._service.setText(
            f"Voice Service: {state} ({health.get('url', '')}"
            + (f", {latency}ms" if latency is not None else "")
            + ")"
        )
        self._detail.setText(
            f"enabled={health.get('enabled')} auto_play={health.get('auto_play')} "
            f"source={health.get('source')}"
        )
        self._hint.setText(f"配置: {config_path()}")

    # -- 勾选 -------------------------------------------------------------------

    def _on_enabled_toggled(self, checked: bool) -> None:
        self._plugin.enable() if checked else self._plugin.disable()
        self.refresh()

    def _on_auto_play_toggled(self, checked: bool) -> None:
        self._plugin.set_auto_play(bool(checked))
        self.refresh()

    # -- 服务按钮 -----------------------------------------------------------------

    def _on_start(self) -> None:
        result = self._plugin.start_service()
        self._detail.setText(f"start: {result}")
        self.refresh()

    def _on_stop(self) -> None:
        result = self._plugin.stop_service()
        self._detail.setText(f"stop: {result}")
        self.refresh()

    def _on_test(self) -> None:
        result = self._plugin.test_play()
        self._detail.setText(f"test_play: {result}")
