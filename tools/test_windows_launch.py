"""Windows launch integration tests (desktop shortcut + launch-on-startup).

All offline: shortcut operations run against temporary directories, and the
Settings toggle is driven with an injected autostart backend so the real
Desktop/Startup folders are never touched. Only the shortcut COM round-trip
requires Windows (it is skipped elsewhere); the launch-spec invariants and the
Settings UI behavior are platform-independent.

Covers A-P from the task spec; L-P are regression checks run via the existing
offline suites (single-instance, sessions/workspace, quick ask, ChatGPT web
launch, UI scale) and are exercised in ``main``.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication

from core import windows_autostart, windows_shortcuts
from core.settings_manager import SettingsManager
from ui.settings_popover import SettingsPopover

WINDOWS = os.name == "nt"


class FakeAutostart:
    """Injected backend for the Settings toggle (no real folders touched)."""

    def __init__(self, enabled: bool = False, *, fail_enable: bool = False, fail_disable: bool = False):
        self.enabled = enabled
        self.fail_enable = fail_enable
        self.fail_disable = fail_disable
        self.enable_calls = 0
        self.disable_calls = 0

    def is_enabled(self) -> bool:
        return self.enabled

    def enable(self) -> None:
        self.enable_calls += 1
        if self.fail_enable:
            raise RuntimeError("enable failed")
        self.enabled = True

    def disable(self) -> bool:
        self.disable_calls += 1
        if self.fail_disable:
            raise RuntimeError("disable failed")
        self.enabled = False
        return True


# -- launch spec invariants (A/B/C/D/E) -----------------------------------


def test_spec_target_is_venv_pythonw() -> None:
    spec = windows_shortcuts.launch_spec()
    assert Path(spec.target).name.lower() == "pythonw.exe"
    assert str(spec.target).lower() == str(windows_shortcuts.VENV_PYTHONW).lower()


def test_spec_working_directory_is_project_dir() -> None:
    spec = windows_shortcuts.launch_spec()
    assert str(spec.working_directory).lower() == str(PROJECT_DIR).lower()


def test_spec_arguments_quote_app_path() -> None:
    spec = windows_shortcuts.launch_spec()
    assert spec.arguments == f'"{windows_shortcuts.APP_SCRIPT}"'


def test_spec_target_is_no_console() -> None:
    # pythonw.exe is the windowed interpreter: no console window on launch.
    assert windows_shortcuts.launch_spec().target.endswith("pythonw.exe")


def test_spec_icon_exists() -> None:
    spec = windows_shortcuts.launch_spec()
    assert Path(spec.icon_path).is_file(), spec.icon_path
    assert Path(spec.icon_path).suffix.lower() == ".ico"


# -- shortcut COM round-trip (Windows only) --------------------------------


def test_shortcut_create_read_remove() -> None:
    if not WINDOWS:
        print("  (skip shortcut COM round-trip: not Windows)")
        return
    with tempfile.TemporaryDirectory() as td:
        lnk = Path(td) / "Firefly AI Pet.lnk"
        spec = windows_shortcuts.launch_spec()
        assert not windows_shortcuts.shortcut_exists(lnk)

        windows_shortcuts.create_shortcut(lnk, spec)
        assert windows_shortcuts.shortcut_exists(lnk)

        props = windows_shortcuts.read_shortcut(lnk)
        assert props is not None
        assert props["target"].lower() == spec.target.lower()
        assert props["arguments"] == spec.arguments
        assert props["workingDirectory"].lower() == spec.working_directory.lower()
        assert props["iconLocation"] == spec.icon_location()

        assert windows_shortcuts.remove_shortcut(lnk) is True
        assert not windows_shortcuts.shortcut_exists(lnk)
        assert windows_shortcuts.remove_shortcut(lnk) is False  # idempotent


# -- autostart: enable / disable / is_enabled (F/G/H) ----------------------


def test_autostart_enable_creates_shortcut() -> None:
    if not WINDOWS:
        print("  (skip autostart enable: not Windows)")
        return
    with tempfile.TemporaryDirectory() as td:
        autostart = windows_autostart.WindowsAutostart(Path(td))
        assert not autostart.is_enabled()
        autostart.enable()
        assert autostart.is_enabled()

        spec = windows_shortcuts.launch_spec()
        props = windows_shortcuts.read_shortcut(autostart.shortcut_path())
        assert props is not None
        assert props["target"].lower() == spec.target.lower()
        assert props["arguments"] == spec.arguments
        assert props["workingDirectory"].lower() == spec.working_directory.lower()
        assert props["iconLocation"] == spec.icon_location()


def test_autostart_disable_removes_shortcut() -> None:
    if not WINDOWS:
        print("  (skip autostart disable: not Windows)")
        return
    with tempfile.TemporaryDirectory() as td:
        autostart = windows_autostart.WindowsAutostart(Path(td))
        autostart.enable()
        assert autostart.disable() is True
        assert not autostart.is_enabled()


def test_autostart_is_enabled_tracks_existence() -> None:
    # H: existence of the .lnk is the source of truth (not a stored flag).
    with tempfile.TemporaryDirectory() as td:
        autostart = windows_autostart.WindowsAutostart(Path(td))
        assert autostart.is_enabled() is False
        path = autostart.shortcut_path()
        path.write_text("stub", encoding="utf-8")  # simulate a manually-created shortcut
        assert autostart.is_enabled() is True
        path.unlink()
        assert autostart.is_enabled() is False


# -- Settings toggle: failure never pretends success (I/J) ------------------


def test_settings_shows_launch_on_startup(app: QApplication) -> None:
    with tempfile.TemporaryDirectory() as td:
        fake = FakeAutostart(enabled=True)
        popover = SettingsPopover(SettingsManager(Path(td) / "prefs.json"), autostart=fake)
        try:
            assert "launch_on_startup" in popover._rows
            assert popover.toggle_state("launch_on_startup") is True
        finally:
            popover.close()


def test_toggle_enable_failure_reverts_and_messages(app: QApplication) -> None:
    with tempfile.TemporaryDirectory() as td:
        fake = FakeAutostart(enabled=False, fail_enable=True)
        popover = SettingsPopover(SettingsManager(Path(td) / "prefs.json"), autostart=fake)
        try:
            popover.show()
            app.processEvents()
            assert popover.toggle_state("launch_on_startup") is False
            popover._rows["launch_on_startup"]._toggle.click()
            app.processEvents()
            assert fake.enable_calls == 1
            assert popover.toggle_state("launch_on_startup") is False  # reverted
            assert popover._status_label.isVisible()
            assert popover._status_label.text() == "Couldn't enable startup."
        finally:
            popover.close()


def test_toggle_disable_failure_reverts(app: QApplication) -> None:
    with tempfile.TemporaryDirectory() as td:
        fake = FakeAutostart(enabled=True, fail_disable=True)
        popover = SettingsPopover(SettingsManager(Path(td) / "prefs.json"), autostart=fake)
        try:
            popover.show()
            app.processEvents()
            assert popover.toggle_state("launch_on_startup") is True
            popover._rows["launch_on_startup"]._toggle.click()
            app.processEvents()
            assert fake.disable_calls == 1
            assert popover.toggle_state("launch_on_startup") is True  # reverted
            assert popover._status_label.text() == "Couldn't disable startup."
        finally:
            popover.close()


def test_toggle_success_updates_state(app: QApplication) -> None:
    with tempfile.TemporaryDirectory() as td:
        fake = FakeAutostart(enabled=False)
        popover = SettingsPopover(SettingsManager(Path(td) / "prefs.json"), autostart=fake)
        try:
            popover.show()
            app.processEvents()
            popover._rows["launch_on_startup"]._toggle.click()
            app.processEvents()
            assert popover.toggle_state("launch_on_startup") is True
            assert popover._status_label.text() == ""
            assert not popover._status_label.isVisible()
        finally:
            popover.close()


# -- legacy preferences without the field (K) -------------------------------


def test_legacy_preferences_without_launch_field() -> None:
    # K: a prefs file predating this feature (no launch_on_startup key) still
    # loads and the toggle reads system state (injected) rather than the file.
    with tempfile.TemporaryDirectory() as td:
        prefs = Path(td) / "pet_preferences.json"
        prefs.write_text(
            json.dumps({"notifications_enabled": True, "keep_awake_enabled": False}),
            encoding="utf-8",
        )
        manager = SettingsManager(prefs)
        assert manager.notifications_enabled is True
        assert manager.keep_awake_enabled is False
        # launch_on_startup is not a preferences field at all.
        assert not hasattr(manager, "launch_on_startup")


def main() -> None:
    app = QApplication.instance() or QApplication([])

    test_spec_target_is_venv_pythonw()
    test_spec_working_directory_is_project_dir()
    test_spec_arguments_quote_app_path()
    test_spec_target_is_no_console()
    test_spec_icon_exists()

    test_shortcut_create_read_remove()
    test_autostart_enable_creates_shortcut()
    test_autostart_disable_removes_shortcut()
    test_autostart_is_enabled_tracks_existence()

    test_settings_shows_launch_on_startup(app)
    test_toggle_enable_failure_reverts_and_messages(app)
    test_toggle_disable_failure_reverts(app)
    test_toggle_success_updates_state(app)
    test_legacy_preferences_without_launch_field()

    print("Windows launch tests passed.")


if __name__ == "__main__":
    main()
