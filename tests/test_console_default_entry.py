"""Default chat entry switch test: Ask… now opens the UI V2 console.

Verifies that ``VisualShell._on_short_ask_requested`` routes to
``ui.v2.console.open_singleton`` with the shared character runner, keeps the
legacy CompanionChatWindow as a fallback, and never breaks on failure.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from app import VisualShell
from ui.v2.console import CompanionConsole

CONSOLE_SENTINEL = "console-opened"


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    app = QApplication.instance() or QApplication([])
    return app


class _StubShell:
    """Minimal VisualShell-like object exercising _on_short_ask_requested."""

    def __init__(self):
        self.character_conversation = SimpleNamespace(
            agent_event=None, ask=lambda text: True,
            session_video=None, video_study=None,
        )
        self._opened: list[str] = []
        self._fallback: list[str] = []

    def _log_console_open_failure(self, exc: Exception) -> None:
        self._fallback.append(str(exc))


def test_ask_opens_console_with_shared_runner(monkeypatch):
    shell = _StubShell()
    captured: dict = {}

    def fake_open_singleton(runner):
        captured["runner"] = runner
        return "console"

    monkeypatch.setattr("ui.v2.console.open_singleton", fake_open_singleton)
    monkeypatch.setattr("ui.companion_chat_window.CompanionChatWindow.open_singleton",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("legacy must not open")))

    VisualShell._on_short_ask_requested(shell)

    assert captured["runner"] is shell.character_conversation
    assert shell._fallback == []


def test_ask_falls_back_to_legacy_window_on_console_failure(monkeypatch):
    shell = _StubShell()

    def failing_open(runner):
        raise RuntimeError("console broken")
    monkeypatch.setattr("ui.v2.console.open_singleton", failing_open)

    legacy_calls: list[dict] = []

    def legacy_open_singleton(**kwargs):
        legacy_calls.append(kwargs)
        return "legacy"
    monkeypatch.setattr("ui.companion_chat_window.CompanionChatWindow.open_singleton",
                        legacy_open_singleton)

    VisualShell._on_short_ask_requested(shell)

    assert len(legacy_calls) == 1
    assert legacy_calls[0]["runner"] is shell.character_conversation
    assert shell._fallback  # failure was logged, not raised


def test_console_singleton_reuses_one_instance(monkeypatch):
    """open_singleton returns the same instance on second call."""
    from ui.v2.console import open_singleton

    shell = _StubShell()
    first = open_singleton(shell.character_conversation)
    second = open_singleton(shell.character_conversation)
    assert first is second
    assert isinstance(first, CompanionConsole)
    first.close()


def test_open_singleton_window_survives_gc():
    """Regression: the console must stay alive after the caller drops the
    open_singleton() return value (app._on_short_ask_requested does exactly
    this). A Python-only reference would let PySide6 garbage-collect the
    QMainWindow, making Ask appear to do nothing with no traceback.
    """
    import gc

    from ui.v2.console import CompanionConsole, open_singleton

    shell = _StubShell()
    win = open_singleton(shell.character_conversation)
    del win  # production discards the reference
    gc.collect()
    found = [
        w for w in QApplication.instance().topLevelWidgets()
        if isinstance(w, CompanionConsole)
    ]
    assert len(found) == 1, (
        "console window was garbage-collected after the caller dropped the reference"
    )
    found[0].close()  # closeEvent clears the module singleton
    assert len([
        w for w in QApplication.instance().topLevelWidgets()
        if isinstance(w, CompanionConsole)
    ]) == 1  # closed (hidden) window still owned until re-opened fresh


# ---------------------------------------------------------------------------
# Phase UI-3A.5: host wiring — the real runtime_bus reaches the console
# ---------------------------------------------------------------------------

class _FakeRuntimeBus:
    def __init__(self):
        self.callbacks: list = []
        self.unsubscribed = 0

    def subscribe_event(self, callback):
        self.callbacks.append(callback)
        return self._unsubscribe

    def _unsubscribe(self):
        self.unsubscribed += 1
        if self.callbacks:
            self.callbacks.clear()


def test_open_singleton_binds_runtime_bus_at_creation():
    bus = _FakeRuntimeBus()
    shell = _StubShell()
    console = __import__("ui.v2.console", fromlist=["open_singleton"]).open_singleton(
        shell.character_conversation, runtime_bus=bus)
    assert console._runtime_unsubscribe is not None
    assert len(bus.callbacks) == 1  # subscribed to runtime.activity
    console.close()
    assert bus.unsubscribed == 1  # closeEvent released the subscription


def test_open_singleton_reuse_keeps_original_bus():
    bus = _FakeRuntimeBus()
    shell = _StubShell()
    first = __import__("ui.v2.console", fromlist=["open_singleton"]).open_singleton(
        shell.character_conversation)  # created without a bus
    second = __import__("ui.v2.console", fromlist=["open_singleton"]).open_singleton(
        shell.character_conversation, runtime_bus=bus)
    assert first is second
    assert first._runtime_unsubscribe is None  # creation-time binding only
    assert bus.callbacks == []  # no new subscription on reuse
    first.close()


def test_host_wiring_passes_real_runtime_bus(monkeypatch):
    """VisualShell._ensure_companion_console must forward the shell's real
    RuntimeBus into open_singleton."""
    from PySide6.QtCore import Signal

    shell = _StubShell()
    shell.runtime_bus = _FakeRuntimeBus()
    shell._on_console_settings = lambda: None
    shell._on_console_research = lambda: None
    shell.open_learning_bridge = lambda: None
    shell._bind_learning_console = lambda console: None

    captured: dict = {}

    from PySide6.QtCore import QObject, Signal as QtSignal

    class _FakeConsole(QObject):
        settings_requested = QtSignal()
        research_requested = QtSignal()
        learning_bridge_requested = QtSignal()

    fake_console = _FakeConsole()

    def fake_open_singleton(runner=None, runtime_bus=None):
        captured["runner"] = runner
        captured["runtime_bus"] = runtime_bus
        return fake_console

    # Patch the exact import target used inside app.py's wiring method.
    import ui.v2.console as console_mod
    original = console_mod.open_singleton
    console_mod.open_singleton = fake_open_singleton
    try:
        VisualShell._ensure_companion_console(shell)
    finally:
        console_mod.open_singleton = original

    assert captured["runner"] is shell.character_conversation
    assert captured["runtime_bus"] is shell.runtime_bus


@pytest.fixture(autouse=True)
def _isolated_console_windows(_qapp):
    """Dispose test-created windows; singleton tests must not inherit prior cases."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from ui.v2 import console as console_module
    def dispose():
        console_module._console_instance = None
        for widget in list(_qapp.topLevelWidgets()):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _qapp.processEvents()
    dispose()
    yield
    dispose()
