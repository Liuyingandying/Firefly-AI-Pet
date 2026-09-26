"""Launcher presence dot tests (green = discovered, hidden = absent).

Deterministic: availability is stubbed BEFORE the dock is constructed, and
tests drive presence_scan_sync() directly — no background thread races.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from ui import agent_launcher
from ui.agent_dock import AgentDock


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _set_availability(monkeypatch, *, codex: bool, qwen: bool, zcode: bool) -> None:
    monkeypatch.setattr(agent_launcher, "codex_available", lambda: codex)
    monkeypatch.setattr(agent_launcher, "qwen_available", lambda: qwen)
    monkeypatch.setattr(agent_launcher, "zcode_available", lambda: zcode)


def _settle(dock: AgentDock, qapp) -> None:
    """排干后台发现线程的迟到投递，再断言。"""
    if dock._presence_thread is not None:
        dock._presence_thread.join(timeout=10)
    qapp.processEvents()


def test_presence_dots_track_discovery(qapp, monkeypatch):
    _set_availability(monkeypatch, codex=True, qwen=False, zcode=True)
    dock = AgentDock()
    try:
        dock._deepseek_ready = False  # 隔离真实凭据库的回退状态
        dock._zhipu_ready = False
        dock.presence_scan_sync()
        _settle(dock, qapp)
        assert set(dock._presence_dots) == {"codex", "qwen", "zcode"}
        assert not dock._presence_dots["codex"].isHidden()
        assert dock._presence_dots["qwen"].isHidden()
        assert not dock._presence_dots["zcode"].isHidden()

        _set_availability(monkeypatch, codex=False, qwen=True, zcode=False)
        dock.presence_scan_sync()
        _settle(dock, qapp)
        assert dock._presence_dots["codex"].isHidden()
        assert not dock._presence_dots["qwen"].isHidden()
        assert dock._presence_dots["zcode"].isHidden()
    finally:
        dock.close()


def test_presence_hidden_when_nothing_discovered(qapp, monkeypatch):
    _set_availability(monkeypatch, codex=False, qwen=False, zcode=False)
    dock = AgentDock()
    try:
        dock._deepseek_ready = False
        dock._zhipu_ready = False
        dock.presence_scan_sync()
        _settle(dock, qapp)
        assert all(dot.isHidden() for dot in dock._presence_dots.values())
    finally:
        dock.close()


def test_fallback_provider_lights_qwen_dot(qapp, monkeypatch):
    """回退链上的 DeepSeek/Zhipu 已配置时，即使 qwen CLI 缺失，点也应亮绿。"""
    _set_availability(monkeypatch, codex=False, qwen=False, zcode=False)
    dock = AgentDock()
    try:
        # 强制回退状态为"都不可用"，建立隐藏基线（本机真实凭据库可能已配置）
        dock._deepseek_ready = False
        dock._zhipu_ready = False
        dock.presence_scan_sync()
        _settle(dock, qapp)
        assert dock._presence_dots["qwen"].isHidden()

        # DeepSeek 回退可用 → 点亮
        dock._deepseek_ready = True
        dock._update_qwen_dot()
        _settle(dock, qapp)
        assert not dock._presence_dots["qwen"].isHidden()
    finally:
        dock.close()


def test_real_machine_discovery_smoke(qapp):
    """真机冒烟：本机已发现 Codex（商店版）与 Z Code（桌面应用）。"""
    assert agent_launcher.codex_available() is True
    assert agent_launcher.zcode_available() is True
