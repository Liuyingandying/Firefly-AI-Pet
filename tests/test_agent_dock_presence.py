"""Codex / Z Code presence dot tests (green = discovered, hidden = absent).

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


def _set_availability(monkeypatch, codex: bool, zcode: bool) -> None:
    monkeypatch.setattr(agent_launcher, "codex_available", lambda: codex)
    monkeypatch.setattr(agent_launcher, "zcode_available", lambda: zcode)


def test_presence_dots_track_discovery(qapp, monkeypatch):
    _set_availability(monkeypatch, codex=True, zcode=False)
    dock = AgentDock()
    try:
        dock.presence_scan_sync()
        assert not dock._presence_dots["codex"].isHidden()
        assert dock._presence_dots["zcode"].isHidden()

        _set_availability(monkeypatch, codex=False, zcode=True)
        dock.presence_scan_sync()
        assert dock._presence_dots["codex"].isHidden()
        assert not dock._presence_dots["zcode"].isHidden()
    finally:
        dock.close()


def test_presence_hidden_when_nothing_discovered(qapp, monkeypatch):
    _set_availability(monkeypatch, codex=False, zcode=False)
    dock = AgentDock()
    try:
        dock.presence_scan_sync()
        assert dock._presence_dots["codex"].isHidden()
        assert dock._presence_dots["zcode"].isHidden()
    finally:
        dock.close()


def test_real_machine_discovery_smoke(qapp):
    """真机冒烟：本机装有 Codex（商店版）与 Z Code，两者都应被发现。"""
    assert agent_launcher.codex_available() is True
    assert agent_launcher.zcode_available() is True
