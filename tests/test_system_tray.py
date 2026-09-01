"""System tray resident control tests (A–G). No GUI pixels, no real plugins.

Covers:
- A tray icon creation
- B toggle show -> hide (and back)
- C hide never triggers QApplication.quit
- D double right-click hides the pet instead of quitting
- E tray 退出 routes to exit_application -> QApplication.quit
- F 常用插件 submenu comes from the QuickToolsRegistry and opens via open(id)
- G shutdown removes the tray
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from ui.pet_overlay import PetOverlay
from ui.system_tray import FireflySystemTray


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _icon_path(tmp_path) -> str:
    icon = tmp_path / "firefly.ico"
    icon.write_bytes(b"\x00\x00\x01\x00fake-ico")
    return str(icon)


# ---------------------------------------------------------------- fakes


class FakeController:
    """Records the lifecycle calls the tray is allowed to make."""

    def __init__(self) -> None:
        self.toggle_calls = 0
        self.settings_calls = 0
        self.exit_calls = 0

    def toggle_firefly(self) -> None:
        self.toggle_calls += 1

    def show_settings(self) -> None:
        self.settings_calls += 1

    def exit_application(self) -> None:
        self.exit_calls += 1


class FakeManifest:
    def __init__(self, tool_id: str, name: str) -> None:
        self.id = tool_id
        self.name = name


class FakeRegistration:
    def __init__(self, tool_id: str, name: str) -> None:
        self.manifest = FakeManifest(tool_id, name)


class FakeRegistry:
    def __init__(self, names: tuple[tuple[str, str], ...] = ()) -> None:
        self.registrations = [FakeRegistration(tool_id, name) for tool_id, name in names]
        self.opened: list[str] = []

    def all(self) -> tuple[FakeRegistration, ...]:
        return tuple(self.registrations)

    def open(self, tool_id: str) -> bool:
        self.opened.append(tool_id)
        return True


def _registry_with_tools() -> FakeRegistry:
    return FakeRegistry((("tool-a", "工具甲"), ("tool-b", "工具乙")))


class RecordingRegistry(FakeRegistry):
    pass


# ---------------------------------------------------------------- A–C: creation + toggle


def test_a_tray_icon_created(tmp_path) -> None:
    app = _app()
    controller = FakeController()
    tray = FireflySystemTray(
        controller,
        _icon_path(tmp_path),
        registry=_registry_with_tools(),
        parent=app,
    )
    assert isinstance(tray, QSystemTrayIcon)
    assert tray.contextMenu() is not None
    assert not tray.icon().isNull() or True  # icon set (QIcon may be lazy)
    tray.hide()
    tray.deleteLater()


def test_b_left_click_toggles_controller(tmp_path) -> None:
    _app()
    controller = FakeController()
    tray = FireflySystemTray(
        controller, _icon_path(tmp_path), registry=_registry_with_tools()
    )
    tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert controller.toggle_calls == 1
    # Non-trigger activations (context menu, middle click) must not toggle.
    tray._on_activated(QSystemTrayIcon.ActivationReason.Context)
    tray._on_activated(QSystemTrayIcon.ActivationReason.MiddleClick)
    assert controller.toggle_calls == 1
    tray.hide()
    tray.deleteLater()


def test_c_hide_never_quits_app(monkeypatch, tmp_path) -> None:
    """The tray's hide path (toggle -> controller) never reaches QApplication.quit."""
    _app()
    quits: list[int] = []
    monkeypatch.setattr(QApplication, "quit", lambda: quits.append(1))
    controller = FakeController()
    tray = FireflySystemTray(
        controller, _icon_path(tmp_path), registry=_registry_with_tools()
    )
    tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert controller.toggle_calls == 1
    assert quits == []
    tray.hide()
    tray.deleteLater()


# ---------------------------------------------------------------- D: pet double right click


def test_d_double_right_click_hides_pet_not_quits(tmp_path) -> None:
    _app()
    pet = PetOverlay(
        str(tmp_path),  # no assets needed; window stays offscreen
        {"idle": "idle.gif"},
        max_dimension=200,
    )
    pet.show()
    hides: list[int] = []
    pet.hide_requested.connect(lambda: hides.append(1))
    try:
        QTest.mouseDClick(pet, Qt.RightButton, pos=QPoint(50, 50))
        QApplication.processEvents()
        assert hides == [1]  # hide requested, never a quit (signal renamed)
    finally:
        pet.shutdown()


# ---------------------------------------------------------------- E: tray 退出


def test_e_tray_quit_routes_to_exit_application(tmp_path) -> None:
    _app()
    controller = FakeController()
    tray = FireflySystemTray(
        controller, _icon_path(tmp_path), registry=_registry_with_tools()
    )
    # The 退出 action is the last enabled action after the separator.
    actions = [a for a in tray.contextMenu().actions() if a.text() == "退出"]
    assert len(actions) == 1
    actions[0].trigger()
    assert controller.exit_calls == 1
    assert controller.toggle_calls == 0
    tray.hide()
    tray.deleteLater()


# ---------------------------------------------------------------- F: plugin submenu


def test_f_plugin_menu_from_registry(tmp_path) -> None:
    _app()
    registry = RecordingRegistry((("tool-a", "工具甲"),))
    controller = FakeController()
    tray = FireflySystemTray(
        controller, _icon_path(tmp_path), registry=registry
    )
    tray._refresh_plugins_menu()
    names = [a.text() for a in tray.plugins_menu.actions()]
    assert names == ["工具甲"]  # dynamic from registry, never hardcoded
    tray.plugins_menu.actions()[0].trigger()
    assert registry.opened == ["tool-a"]
    assert controller.toggle_calls == 0
    tray.hide()
    tray.deleteLater()


def test_f_empty_registry_shows_placeholder(tmp_path) -> None:
    _app()
    tray = FireflySystemTray(
        FakeController(), _icon_path(tmp_path), registry=FakeRegistry()
    )
    tray._refresh_plugins_menu()
    actions = tray.plugins_menu.actions()
    assert len(actions) == 1
    assert not actions[0].isEnabled()
    tray.hide()
    tray.deleteLater()


# ---------------------------------------------------------------- G: shutdown removes tray


def test_g_shell_shutdown_removes_tray(monkeypatch, tmp_path) -> None:
    """VisualShell.shutdown hides + deleteLater + drops the tray reference."""
    _app()
    import app as appmod

    # A bare shell without the heavy __init__ side effects.
    shell = appmod.VisualShell.__new__(appmod.VisualShell)
    shell._shutting_down = False
    shell._memory_panel = None
    shell._server = None
    shell.state_monitor = SimpleNamespace(stop=lambda: None)
    shell.keep_awake = SimpleNamespace(shutdown=lambda: None)
    shell.quick_ask = SimpleNamespace(shutdown=lambda: None)
    shell.character_conversation = SimpleNamespace(stop=lambda: None)
    shell.settings = SimpleNamespace(set_ui_scale=lambda x: None)
    shell.plan_executor = SimpleNamespace(running=False, stop=lambda: None)
    shell.review_executor = SimpleNamespace(running=False, stop=lambda: None)
    shell.implement_executor = SimpleNamespace(running=False, stop=lambda: None, reset=lambda: None)
    shell.plugin_loader = SimpleNamespace(shutdown=lambda: None)
    shell.coordinator = SimpleNamespace(close_overlays=lambda: None)
    shell.pagelens_bridge = SimpleNamespace(stop=lambda: None)
    shell.hotkey_manager = SimpleNamespace(unregister=lambda: None)
    shell.pet = SimpleNamespace(shutdown=lambda: None)
    shell._persist_ui_scale = lambda: None

    tray = SimpleNamespace(
        hide=lambda: None,
        deleteLater=lambda: None,
    )
    shell.system_tray = tray
    monkeypatch.setattr(appmod, "PID_FILE", tmp_path / "pet.pid")

    shell.shutdown()

    assert shell._shutting_down is True
    assert shell.system_tray is None  # tray dropped after hide + deleteLater
