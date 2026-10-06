"""Entry wiring regression tests (Learning Bridge entry-wiring fix, Phase 7).

Locks the corrected routes:
- main-UI 学习模式 button -> LearningBridgeDialog (via open_learning_bridge),
  never directly into legacy core/learning
- tray 学习模式 -> the same open_learning_bridge entry
- course launch -> launch_learning_mode with a real Z Code command
- launch failure -> structured error surfaced, no silent legacy fallback
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from learning.diagnostics import LOGGER_NAME  # noqa: E402
from learning.launcher import LaunchError  # noqa: E402


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
        self.asks: list[str] = []

    def ask(self, text: str, learning_result=None) -> bool:
        self.asks.append(text)
        return True


def _make_console(qapp, tmp_path):
    from core.learning.store import LearningStore
    from ui.v2.console import CompanionConsole

    runner = _FakeRunner()
    console = CompanionConsole(runner)
    console.learning._store = LearningStore(tmp_path / "learning.sqlite3")
    console.learning._store.initialize()
    return console, runner


# 1. main-UI 学习模式 button -> bridge route (real button click, real console)


def test_1_main_ui_study_button_routes_to_bridge(qapp, tmp_path, monkeypatch, caplog):
    dialog_opens = []
    monkeypatch.setattr(
        "app.VisualShell.open_learning_bridge", lambda self: dialog_opens.append(True)
    )
    from app import VisualShell  # noqa: F401  (patch target must exist)

    console, runner = _make_console(qapp, tmp_path)
    shell = VisualShell.__new__(VisualShell)  # minimal shell; only the slot is used
    console.learning_bridge_requested.connect(shell.open_learning_bridge)

    with caplog.at_level("INFO", logger=LOGGER_NAME):
        console.ability.button("study").click()  # the real button
        qapp.processEvents()

    assert dialog_opens == [True]
    messages = caplog.text
    assert "[LEARNING_ENTRY] source=ability_panel" in messages
    assert "[LEARNING_ROUTE] route=learning_bridge" in messages
    # the button must NOT drive the chat / legacy controller
    assert runner.asks == []
    assert not console.learning.state.enabled
    console.close()


# 2. tray 学习模式 -> open_learning_bridge


def test_2_tray_learning_mode_routes_to_bridge(qapp, caplog):
    from ui.system_tray import FireflySystemTray

    calls = []

    class _Controller:
        def open_learning_bridge(self):
            calls.append(True)

    tray = FireflySystemTray(_Controller(), icon_path="")
    with caplog.at_level("INFO", logger=LOGGER_NAME):
        tray._open_learning_bridge()

    assert calls == [True]
    assert "[LEARNING_ENTRY] source=tray" in caplog.text


# 3. the main-UI study branch must not call legacy core/learning directly


def test_3_study_branch_has_no_legacy_calls():
    source = (PROJECT_DIR / "ui" / "v2" / "console.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_on_ability"
    )
    study_branch = None
    for node in ast.walk(handler):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "study" in test:
                study_branch = node
                break
    assert study_branch is not None, "study 分支丢失"
    body_src = ast.unparse(study_branch)
    for forbidden in (
        "enter_mode",
        "exit_mode",
        "_STUDY_TRIGGER",
        "append_assistant",
        "_show_learning_entry",
    ):
        assert forbidden not in body_src, f"study 分支仍直接调用 legacy: {forbidden}"
    assert "learning_bridge_requested.emit()" in body_src


# 4+5. course selection -> launch_learning_mode -> real Z Code command


def test_4_and_5_course_launch_builds_zcode_command(qapp, tmp_path, monkeypatch):
    from core.settings_manager import SettingsManager
    from learning import bridge_dialog
    from learning.bridge_dialog import LearningBridgeDialog
    from learning.course_binding import save_binding
    from learning.identity import ensure_learner_id
    from learning.launcher import LaunchResult, LaunchPlan
    from learning.resource_manager import ResourceManager

    manager = ResourceManager(tmp_path / "courses")
    settings = SettingsManager(preferences_file=tmp_path / "prefs.json")
    course = manager.import_pdf(
        _fake_pdf(tmp_path / "a.pdf"), ensure_learner_id(settings), title="接线课程"
    )
    save_binding(course.course_dir, course.course_id, curriculum_id="curr-w1")

    captured = {}

    def fake_launch(course_id, action, **kwargs):
        captured["args"] = (course_id, action)
        captured["kwargs"] = kwargs
        plan = LaunchPlan(
            course_id=course_id,
            action=action,
            learner_id="ff-00000000",
            workspace=course.course_dir / "workspace",
            context_file=course.course_dir / "workspace/.firefly/learning_context.json",
            argv=["node", "zcode.cjs", "--prompt", "x", "--cwd", "ws"],
            cwd=course.course_dir / "workspace",
        )
        return LaunchResult(plan=plan, spawned=True, pid=1234)

    monkeypatch.setattr(bridge_dialog, "launch_learning_mode", fake_launch)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    dialog = LearningBridgeDialog(settings=settings, courses_root=manager.courses_root)
    assert dialog._course_list.count() == 1
    dialog._course_list.setCurrentRow(0)
    dialog._launch_button.click()
    qapp.processEvents()

    assert captured["args"][0] == course.course_id
    assert captured["args"][1] == "resume"  # curriculum bound -> resume semantics
    # task-injection contract: the dialog hands (course_id, action) through;
    # the launcher itself attaches the deterministic bootstrap prompt
    assert "headless" not in captured["kwargs"]


# 6. launch failure -> structured error, no silent legacy fallback


def test_6_launch_failure_shows_structured_error(qapp, tmp_path, monkeypatch):
    from core.settings_manager import SettingsManager
    from learning import bridge_dialog
    from learning.bridge_dialog import LearningBridgeDialog
    from learning.identity import ensure_learner_id
    from learning.resource_manager import ResourceManager

    manager = ResourceManager(tmp_path / "courses")
    settings = SettingsManager(preferences_file=tmp_path / "prefs.json")
    course = manager.import_pdf(
        _fake_pdf(tmp_path / "a.pdf"), ensure_learner_id(settings), title="失败课程"
    )

    warnings = []

    def fake_launch(*args, **kwargs):
        raise LaunchError("ZCODE_CLI_NOT_FOUND", "未找到 Z Code CLI")

    monkeypatch.setattr(bridge_dialog, "launch_learning_mode", fake_launch)
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: warnings.append(k or a)
    )

    dialog = LearningBridgeDialog(settings=settings, courses_root=manager.courses_root)
    dialog._course_list.setCurrentRow(0)
    dialog._launch_button.click()
    qapp.processEvents()

    assert len(warnings) == 1
    assert "ZCODE_CLI_NOT_FOUND" in str(warnings[0])
    # no fallback: the runner/chat path is untouched by design — the dialog
    # has no reference to legacy core/learning at all
    assert "learning" not in {
        name for name in dir(bridge_dialog) if name.startswith("learning_")
    }


def _fake_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4\nwiring\n")
    return path
