"""firefly_voice 插件验收测试（7 项）。

1. 插件发现成功
2. 配置迁移成功
3. 旧 voice_config 兼容
4. enabled=false: 不会调用 voice 服务
5. enabled=true: 可以检测服务状态
6. auto_play=false: 保持按钮播放
7. 无 voice_module: 主程序正常运行（各组件优雅降级）

隔离原则：全部测试在临时用户数据根（FIREFLY_USER_DATA_DIR）与临时插件根下运行，
不触碰真实配置与服务；不修改任何核心测试。
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parents[2]
PLUGIN_SRC = PROJECT_DIR / "extensions" / "firefly_voice"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def user_root(tmp_path, monkeypatch):
    """隔离的用户数据根（插件配置落点）。

    注意：``core.user_paths.DEFAULT_USER_PATHS`` 是导入期冻结的模块常量，
    仅设 FIREFLY_USER_DATA_DIR 环境变量无效——必须直接替换该常量。
    """
    import core.user_paths as user_paths

    root = tmp_path / "userdata"
    monkeypatch.setattr(user_paths, "DEFAULT_USER_PATHS",
                        user_paths.UserDataPaths(root=root))
    # 清掉解析缓存，避免跨测试污染
    import voice_client.config as vc

    vc._MANAGED_CACHE.update({"key": None, "config": None})
    yield root
    vc._MANAGED_CACHE.update({"key": None, "config": None})


@pytest.fixture()
def legacy_file(tmp_path, monkeypatch):
    """隔离的旧配置文件（替代仓库内 config/voice_config.yaml）。"""
    path = tmp_path / "legacy_voice_config.yaml"
    import voice_client.config as vc

    monkeypatch.setattr(vc, "CONFIG_PATH", path)
    return path


@pytest.fixture()
def plugin_root(tmp_path, monkeypatch):
    """只含 firefly_voice 的临时插件根（避免加载其它官方插件）。"""
    root = tmp_path / "plugin_root"
    root.mkdir()
    shutil.copytree(PLUGIN_SRC, root / "firefly_voice",
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    monkeypatch.setenv("FIREFLY_PLUGIN_PATH", str(root))
    yield root


def _write_legacy(path: Path, enabled: bool, url: str = "http://127.0.0.1:8300",
                  max_chars: int = 1600) -> None:
    path.write_text(
        "voice:\n"
        f"  enabled: {str(enabled).lower()}\n"
        "  auto_play: false\n"
        f"  max_speak_chars: {max_chars}\n"
        "  server:\n"
        f"    url: \"{url}\"\n"
        "    connect_timeout_s: 2.0\n"
        "    read_timeout_s: 30.0\n"
        "  emotion:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )


# 1. 插件发现成功 --------------------------------------------------------------

def test_1_plugin_discovery(qapp, plugin_root):
    from core.plugin_loader import MANAGED_PLUGIN_IDS, PluginLoader
    from core.quick_tools import QuickToolsRegistry

    assert "firefly-voice" in MANAGED_PLUGIN_IDS

    registry = QuickToolsRegistry()
    loader = PluginLoader(registry=registry)
    manifests = loader.load()
    ids = [m.id for m in manifests]
    assert "firefly-voice" in ids, f"discovered={ids}"

    reg = registry.get("firefly-voice")
    assert reg is not None
    assert reg.manifest.name == "Voice"
    assert "voice" in reg.manifest.capabilities
    loader.shutdown()


# 2. 配置迁移成功 --------------------------------------------------------------

def test_2_config_migration_creates_user_config(user_root, legacy_file):
    from voice_client.config import load_voice_config, managed_user_config_path

    _write_legacy(legacy_file, enabled=False)  # 旧文件是关闭态（故障语义）
    user_path = managed_user_config_path()
    assert not user_path.is_file()

    cfg = load_voice_config()

    assert user_path.is_file(), "迁移应生成用户插件配置"
    assert cfg.enabled is True           # 新默认：语音能力默认存在
    assert cfg.auto_play is False        # v1.3
    assert cfg.source == "managed(migrated)"


# 3. 旧 voice_config 兼容 --------------------------------------------------------

def test_3_legacy_fields_carried_over(user_root, legacy_file):
    from voice_client.config import load_voice_config

    _write_legacy(legacy_file, enabled=False, url="http://127.0.0.1:8399", max_chars=777)
    cfg = load_voice_config()

    assert cfg.url == "http://127.0.0.1:8399"   # 旧文件的非布尔字段被继承
    assert cfg.max_speak_chars == 777
    assert cfg.emotion_enabled is True

    # 用户配置存在后，旧文件改动不再影响解析（用户配置优先）
    _write_legacy(legacy_file, enabled=True, url="http://127.0.0.1:8400")
    cfg2 = load_voice_config()
    assert cfg2.url == "http://127.0.0.1:8399"


# 4. enabled=false: 不会调用 voice 服务 -------------------------------------------

def test_4_disabled_never_calls_service(user_root, legacy_file, monkeypatch):
    from voice_client.client import FireflyVoiceClient
    from voice_client.config import load_voice_config, save_managed_overrides

    _write_legacy(legacy_file, enabled=True)
    load_voice_config()  # 触发迁移
    assert save_managed_overrides({"enabled": False})

    calls = {"n": 0}

    def _forbidden(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("disabled 状态不得发起 HTTP 请求")

    monkeypatch.setattr("urllib.request.urlopen", _forbidden)

    client = FireflyVoiceClient()
    result = client.speak("不应被朗读")

    assert result["ok"] is False and result["reason"] == "disabled"
    assert calls["n"] == 0


# 5. enabled=true: 可以检测服务状态 ------------------------------------------------

def test_5_enabled_detects_service_state(user_root, legacy_file):
    from voice_client.config import load_voice_config, save_managed_overrides
    from firefly_voice.voice_service_manager import VoiceServiceManager

    _write_legacy(legacy_file, enabled=True)
    load_voice_config()
    assert save_managed_overrides({"enabled": True})

    # 起一个临时 TCP 监听模拟服务
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    stop = threading.Event()

    def _serve():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except (socket.timeout, OSError):
                continue

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        save_managed_overrides({"url": f"http://127.0.0.1:{port}"})
        manager = VoiceServiceManager()
        online = manager.check(force=True)
        assert online["online"] is True, online
        assert online["url"] == f"http://127.0.0.1:{port}"
    finally:
        stop.set()
        server.close()

    # 关闭后回到 OFFLINE（探测失败不抛异常）
    save_managed_overrides({"url": "http://127.0.0.1:1"})
    manager = VoiceServiceManager()
    offline = manager.check(force=True)
    assert offline["online"] is False
    assert "offline" in offline["detail"]


# 6. auto_play=false: 保持按钮播放 --------------------------------------------------

def test_6_auto_play_false_keeps_button_only():
    from voice_client.announcer import VoiceAnnouncer
    from voice_client.config import VoiceConfig

    class _StubClient:
        def __init__(self, auto_play: bool):
            self.config = VoiceConfig(enabled=True, auto_play=auto_play)
            self.spoken: list[str] = []

        def speak(self, text, emotion=None, priority=1):
            self.spoken.append(text)
            return {"ok": True, "reason": "stub"}

    event = SimpleNamespace(type=__import__("core.agent_events", fromlist=["AgentEventType"]).AgentEventType.FINAL,
                            text="回复内容")

    stub_off = _StubClient(auto_play=False)
    VoiceAnnouncer(client=stub_off).on_agent_event(event)
    assert stub_off.spoken == []          # 默认：FINAL 不自动朗读（仅按钮）

    stub_on = _StubClient(auto_play=True)
    VoiceAnnouncer(client=stub_on).on_agent_event(event)
    assert len(stub_on.spoken) == 1       # 打开后才自动朗读


def test_8_plugin_conforms_to_firefly_extension(qapp, plugin_root):
    """生态符合性：voice 插件必须继承 FireflyExtension（v2 契约基类）。"""
    from core.extension_api import FireflyExtension
    from core.plugin_api import QuickToolPlugin
    from core.plugin_loader import PluginLoader
    from core.quick_tools import QuickToolsRegistry

    registry = QuickToolsRegistry()
    loader = PluginLoader(registry=registry)
    loader.load()
    plugin = loader.plugin("firefly-voice")
    assert plugin is not None
    assert isinstance(plugin, FireflyExtension)
    assert isinstance(plugin, QuickToolPlugin)
    assert plugin.capabilities == ("voice", "audio")
    assert callable(getattr(plugin, "publish_status", None))
    assert callable(getattr(plugin, "health_check", None))
    loader.shutdown()


# 7. 无 voice_module: 主程序正常运行 ---------------------------------------------------

def test_7_no_service_components_degrade_gracefully(user_root, legacy_file, plugin_root, qapp):
    from voice_client.client import FireflyVoiceClient
    from voice_client.config import load_voice_config, save_managed_overrides
    from core.plugin_loader import PluginLoader
    from core.quick_tools import QuickToolsRegistry
    from firefly_voice.voice_service_manager import VoiceServiceManager

    _write_legacy(legacy_file, enabled=True)
    load_voice_config()
    save_managed_overrides({"enabled": True, "url": "http://127.0.0.1:1"})  # 保证无服务

    # 服务管理：检测/启动失败均优雅
    manager = VoiceServiceManager()
    assert manager.check(force=True)["online"] is False
    start = manager.start()
    assert start["ok"] is False and start["detail"] == "service_not_configured"
    assert manager.stop()["ok"] is False  # 无服务可停

    # 客户端：连接拒绝也是优雅失败
    client = FireflyVoiceClient()
    result = client.speak("无服务时")
    assert result["ok"] is False and result["reason"] in ("unavailable", "disabled")

    # 插件：加载 → 状态 OFFLINE → 健康检查 → 关闭，全程无异常
    registry = QuickToolsRegistry()
    loader = PluginLoader(registry=registry)
    manifests = loader.load()
    plugin = loader.plugin("firefly-voice")
    assert plugin is not None
    assert plugin.status() == "OFFLINE"
    health = plugin.health_check()
    assert health["online"] is False and health["enabled"] is True
    loader.shutdown()
