# -*- coding: utf-8 -*-
"""firefly_voice — Voice 能力插件（Quick Tools 契约）。

职责（只做语音能力状态管理，不碰聊天）：
- status / health_check：服务在线状态（127.0.0.1:8300 TCP 探测）
- enable / disable / set_auto_play：写入用户插件配置（免重启即时生效）
- restart_service：显式停+启外部服务（绝不自动启动）
- open：Voice Settings 对话框（经插件系统暴露，不改聊天界面）

链路（FINAL→Announcer→voice_client→8300）保持原样，本插件不参与。
"""

from __future__ import annotations

import logging

from core.extension_api import FireflyExtension
from core.plugin_api import PluginContext
from core.quick_tools import QuickToolManifest

from firefly_voice.config_schema import (
    config_path,
    load_voice_config,
    set_auto_play,
    set_enabled,
)
from firefly_voice.voice_service_manager import VoiceServiceManager

log = logging.getLogger("firefly.voice.plugin")

PLUGIN_ID = "firefly-voice"
PLUGIN_VERSION = "0.1.0"


class FireflyVoicePlugin(FireflyExtension):
    """Voice capability manager: state + config + explicit service lifecycle.

    Extends the ecosystem-standard ``FireflyExtension`` base (v2 lifecycle:
    initialize → start → open* → stop → shutdown) so status changes also
    publish ``plugin.status`` events on the runtime bus when one is injected.
    """

    capabilities = ("voice", "audio")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.capability_exposed = False
        self._manager = VoiceServiceManager()
        self._dialog = None
        self._client = None  # 惰性：测试播放复用宿主 voice_client

    # -- manifest / capability ------------------------------------------------

    @property
    def manifest(self) -> QuickToolManifest:
        return QuickToolManifest(
            id=PLUGIN_ID,
            name="Voice",
            description="Firefly voice capability: service status, enable/auto-play control",
            icon="volume",
            version=PLUGIN_VERSION,
            capabilities=self.capabilities,
            min_api="2",
            author="Firefly",
        )

    @property
    def capability(self) -> str:
        return "TTS + RVC · managed service · button playback"

    @property
    def capability_note(self) -> str:
        return "The voice service is never started automatically; auto-play stays off by design (v1.3)."

    # -- lifecycle ------------------------------------------------------------

    def initialize(self, context: PluginContext) -> None:
        super().initialize(context)  # FireflyExtension: 保存 context（供 publish_status/context 属性）
        # 触发管理链解析与一次性迁移（幂等；失败哑化在 config 层内）
        load_voice_config()
        self._refresh_status()

    def start(self) -> None:
        """插件被启用（Quick Tools 热生命周期）：只暴露能力，不启动服务。"""
        self.capability_exposed = True
        self._refresh_status()

    def stop(self) -> None:
        self.capability_exposed = False
        self._refresh_status()

    def open(self) -> None:
        """打开 Voice Settings 对话框（模型无 UI 环境时静默跳过）。"""
        try:
            from firefly_voice.ui import VoiceSettingsDialog

            if self._dialog is None:
                self._dialog = VoiceSettingsDialog(self)
            self._dialog.refresh()
            self._dialog.show()
            self._dialog.raise_()
            self._dialog.activateWindow()
        except Exception as exc:  # noqa: BLE001 - UI 失败不影响宿主
            log.warning("voice settings dialog unavailable: %s", exc)

    def shutdown(self) -> None:
        self.capability_exposed = False
        if self._dialog is not None:
            try:
                self._dialog.close()
            except Exception:  # noqa: BLE001
                pass
            self._dialog = None
        # 资源控制策略：服务由用户显式启动，宿主退出不代管其生命周期。

    # -- 状态与健康 -------------------------------------------------------------

    def _refresh_status(self) -> None:
        result = self._manager.check()
        # publish_status: 更新卡片状态位 + （有总线时）发 plugin.status 事件
        self.publish_status("ONLINE" if result["online"] else "OFFLINE",
                            result.get("detail", ""))

    def status(self) -> str:
        """Quick Tools 卡片状态位：ONLINE / OFFLINE（3s 探测缓存）。"""
        result = self._manager.check()
        return "ONLINE" if result["online"] else "OFFLINE"

    def health_check(self) -> dict:
        """完整健康信息（force 探测）。"""
        result = self._manager.check(force=True)
        cfg = load_voice_config()
        return {
            **result,
            "enabled": bool(cfg.enabled),
            "auto_play": bool(cfg.auto_play),
            "source": cfg.source,
        }

    # -- 能力管理 API -------------------------------------------------------------

    def enable(self) -> dict:
        """打开语音能力（写用户插件配置，免重启生效）。"""
        ok = set_enabled(True)
        return {"ok": ok, "enabled": True, "config_path": str(config_path())}

    def disable(self) -> dict:
        ok = set_enabled(False)
        return {"ok": ok, "enabled": False, "config_path": str(config_path())}

    def set_auto_play(self, value: bool) -> dict:
        """设置自动朗读（v1.3 默认 False=仅播放按钮）。"""
        ok = set_auto_play(bool(value))
        return {"ok": ok, "auto_play": bool(value)}

    def restart_service(self) -> dict:
        """显式停+启外部服务（不自动启动策略下的用户动作入口）。"""
        return self._manager.restart()

    def start_service(self) -> dict:
        return self._manager.start()

    def stop_service(self) -> dict:
        return self._manager.stop()

    # -- 测试播放（显式用户动作） -----------------------------------------------------

    def test_play(self, text: str = "你好，我是流萤。") -> dict:
        """经宿主 voice_client 触发一次真实播报（服务需在线）。"""
        try:
            if self._client is None:
                from voice_client.client import FireflyVoiceClient

                self._client = FireflyVoiceClient()
            return self._client.speak(text, priority=2)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"client_error:{type(exc).__name__}"}


def create_plugin(parent=None) -> FireflyVoicePlugin:
    return FireflyVoicePlugin(parent)
