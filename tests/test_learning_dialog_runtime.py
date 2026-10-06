"""Dialog lifetime runtime regression (Phase 9).

Must verify the END STATE — the dialog is strongly referenced by the owner
and ``isVisible() is True`` after a real button click through the real
signal path — not merely "a signal was emitted".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from learning.diagnostics import LOGGER_NAME  # noqa: E402


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeRunner:
    def __init__(self) -> None:
        self.agent_event = None
        self.session_video = None
        self.video_study = None
        self._screen_vision_settings = None
        self.learning_controller = None

    def ask(self, text: str, learning_result=None) -> bool:
        return True


class _ShellStub:
    """Minimal owner holding ONLY the attribute the real slot uses.

    The real ``VisualShell.open_learning_bridge`` is invoked unbound on this
    object, so the actual production slot body runs (markers, singleton
    reference, show/raise/activate, visibility verification).
    """

    def __init__(self) -> None:
        self._learning_bridge_dialog = None
        self._learning_bridge_session = None


@pytest.fixture()
def isolated_courses(monkeypatch, tmp_path):
    """Keep the dialog away from the user's real course data."""
    from learning.resource_manager import ResourceManager

    monkeypatch.setattr(
        "learning.bridge_dialog.ResourceManager",
        lambda courses_root=None: ResourceManager(tmp_path / "courses"),
    )
    return tmp_path / "courses"


@pytest.fixture()
def clean_console_singleton():
    """Snapshot and restore the console module singleton around the test."""
    import ui.v2.console as console_module

    previous = console_module._console_instance
    previous_opener = console_module._learning_bridge_opener
    yield
    console_module._console_instance = previous
    console_module._learning_bridge_opener = previous_opener


def test_real_button_to_visible_dialog(qapp, tmp_path, monkeypatch, caplog,
                                       isolated_courses, clean_console_singleton):
    import ui.v2.console as console_module
    from app import VisualShell
    from core.learning.store import LearningStore
    from learning.bridge_dialog import LearningBridgeDialog
    from learning.resource_manager import ResourceManager
    from ui.v2.console import set_learning_bridge_opener

    # real console via the REAL open_singleton path (Short-Ask entry shape)
    runner = _FakeRunner()
    console = console_module.open_singleton(runner=runner)

    # the shell registered the opener at startup (module-level binding)
    owner = _ShellStub()
    set_learning_bridge_opener(
        lambda: VisualShell.open_learning_bridge(owner)
    )
    console_module._bind_learning_bridge_opener(console)

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        console.ability.button("study").click()  # the real button
        qapp.processEvents()

    dialog = owner._learning_bridge_dialog
    assert isinstance(dialog, LearningBridgeDialog)
    assert dialog.isVisible() is True, "对话框必须最终可见（端到端 lifetime）"
    assert "[LEARNING_SIGNAL_RECEIVE]" in caplog.text
    assert "[LEARNING_DIALOG_CREATE]" in caplog.text
    assert "[LEARNING_DIALOG_SHOW]" in caplog.text
    assert "visible=True" in caplog.text
    dialog.close()
    console.close()


def test_second_click_reuses_singleton_dialog(qapp, monkeypatch, caplog,
                                              isolated_courses, clean_console_singleton):
    import ui.v2.console as console_module
    from app import VisualShell
    from learning.resource_manager import ResourceManager

    console = console_module.open_singleton(runner=_FakeRunner())
    owner = _ShellStub()
    set_learning_bridge_opener = console_module.set_learning_bridge_opener
    set_learning_bridge_opener(lambda: VisualShell.open_learning_bridge(owner))
    console_module._bind_learning_bridge_opener(console)

    console.ability.button("study").click()
    qapp.processEvents()
    first = owner._learning_bridge_dialog
    assert first is not None and first.isVisible()

    console.ability.button("study").click()
    qapp.processEvents()
    assert owner._learning_bridge_dialog is first  # reused, never duplicated
    assert first.isVisible()
    first.close()
    console.close()


def test_constructor_failure_surfaces_error_not_silence(
    qapp, tmp_path, monkeypatch, caplog, isolated_courses, clean_console_singleton
):
    import logging

    import ui.v2.console as console_module
    from app import VisualShell

    def _boom(*a, **k):
        raise RuntimeError("dialog constructor exploded")

    monkeypatch.setattr(
        "learning.bridge_dialog.LearningBridgeDialog", _boom
    )
    shown = []
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a))

    console = console_module.open_singleton(runner=_FakeRunner())
    owner = _ShellStub()
    with caplog.at_level("INFO", logger=LOGGER_NAME):
        console_module.set_learning_bridge_opener(
            lambda: VisualShell.open_learning_bridge(owner)
        )
        console_module._bind_learning_bridge_opener(console)
        console.ability.button("study").click()
        qapp.processEvents()

    assert owner._learning_bridge_dialog is None  # nothing half-constructed
    assert shown  # user-visible structured failure
    assert "[LEARNING_DIALOG_STATE] visible=False error=True" in caplog.text
    console.close()
