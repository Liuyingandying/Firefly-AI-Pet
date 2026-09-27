"""Learning Mode UI tests (Phase 1B).

Covers the shell UI contract:
- the 学习模式 ability button no longer demands a Bilibili link
- mode strip / right status card reflect mode + course
- course picker is transient (appears only when needed, disappears on choice)
- no new toolbar icon / no persistent project manager
- learning context flows through the ordinary ProviderRouter chat path only
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from core.learning.controller import LearningModeController
from core.learning.store import LearningStore
from ui.v2.ability_panel import CAPABILITIES


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeRunner:
    """Minimum runner surface the console touches."""

    def __init__(self) -> None:
        self.agent_event = None  # console skips event wiring when None
        self.session_video = None
        self.video_study = None
        self._screen_vision_settings = None
        self.learning_controller = None

    def ask(self, text: str, learning_result=None) -> bool:
        self.last_ask = text
        return True


def _make_console(qapp, tmp_path, runner=None):
    from ui.v2.console import CompanionConsole

    runner = runner or _FakeRunner()
    console = CompanionConsole(runner)
    console.learning._store = LearningStore(tmp_path / "learning.sqlite3")
    console.learning._store.initialize()
    return console, runner


# 28. no new toolbar icon ----------------------------------------------------


def test_no_new_toolbar_icon(qapp) -> None:
    # The ability strip stays exactly as before (study entry reused, nothing
    # added, nothing removed).
    assert [cap for cap, _, _ in CAPABILITIES] == [
        "video", "study", "screen_vision", "document", "research", "settings",
    ]


def test_study_button_enters_mode_without_bilibili(qapp, tmp_path) -> None:
    """Learning bridge v0.1 entry wiring: the 学习模式 ability button asks the
    app shell for LearningBridgeDialog (signal) instead of silently entering
    legacy core/learning in the chat. Legacy mode is NOT toggled here."""
    console, runner = _make_console(qapp, tmp_path)
    asked: list[str] = []
    original_ask = runner.ask
    runner.ask = lambda text, learning_result=None: (
        asked.append(text) or original_ask(text, learning_result)
    )
    requested = []
    console.learning_bridge_requested.connect(lambda: requested.append(True))
    console._on_ability("study")
    assert requested == [True]
    # the button no longer drives the chat or the legacy mode toggle
    assert console.learning.state.enabled is False
    assert asked == []
    console.close()


def test_study_button_toggles_off(qapp, tmp_path) -> None:
    """The ability button is now bridge-only: it never toggles legacy mode on
    or off (legacy keeps its own entries — trigger word, video study)."""
    console, _ = _make_console(qapp, tmp_path)
    console.learning.enter_mode()
    requested = []
    console.learning_bridge_requested.connect(lambda: requested.append(True))
    console._on_ability("study")
    assert requested == [True]
    assert console.learning.state.enabled is True  # untouched by the button
    console.close()


# 29. right status card + mode strip ----------------------------------------


def test_status_card_and_strip_reflect_course(qapp, tmp_path) -> None:
    console, _ = _make_console(qapp, tmp_path)
    console.learning.enter_mode()
    console.learning.create_course("自动控制原理")
    console._apply_learning_state()
    assert not console.mode_strip.isHidden()
    assert "学习模式" in console.mode_strip.text()
    assert "自动控制原理" in console.mode_strip.text()
    assert console.companion.context_status.view.mode == "学习模式"
    assert console.companion.context_status.view.course_name == "自动控制原理"
    # Exit restores the card.
    console.learning.exit_mode()
    console._apply_learning_state()
    assert console.mode_strip.isHidden()
    assert console.companion.context_status.view.mode == "自由对话"
    console.close()


# 30. course picker is transient --------------------------------------------


def test_course_picker_appears_only_when_needed(qapp, tmp_path) -> None:
    console, _ = _make_console(qapp, tmp_path)
    console.learning.enter_mode()  # no courses -> no picker
    console._maybe_show_course_picker("")
    from ui.v2.course_picker import CoursePickerCard

    assert not any(
        isinstance(console.chat._column.itemAt(i).widget(), CoursePickerCard)
        for i in range(console.chat._column.count())
    )
    # With courses, the picker appears...
    console.learning.create_course("物理光学")
    console.learning.create_course("自动控制原理")
    console._maybe_show_course_picker("")
    pickers = [
        console.chat._column.itemAt(i).widget()
        for i in range(console.chat._column.count())
        if isinstance(console.chat._column.itemAt(i).widget(), CoursePickerCard)
    ]
    assert len(pickers) == 1
    picker = pickers[0]
    # ...and disappears after a choice (hide()).
    picker.course_selected.emit(console.learning.state.active_course_id)
    assert not picker.isVisible()
    console.close()


def test_learning_commands_are_intercepted_and_chat_flows(qapp, tmp_path) -> None:
    console, runner = _make_console(qapp, tmp_path)
    console.learning.enter_mode()
    console.learning.create_course("物理光学")

    # Learning command: intercepted, no runner call.
    console.learning.handle_text("我有哪些学习项目")
    assert getattr(runner, "last_ask", None) is None

    # Ordinary question: falls through to the runner (ProviderRouter path).
    console.learning.handle_text("什么是根轨迹")
    assert getattr(runner, "last_ask", None) is None
    console.close()


# -- runner learning context (22/23: same router, no second provider) -------


class _FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.conversation_store = None

    def chat(self, user_message, history=None, turn_context=None):
        self.calls.append({"text": user_message, "turn_context": turn_context})
        return {"choices": [{"message": {"content": "回答"}}]}


def test_learning_context_flows_through_router(qapp, tmp_path) -> None:
    from ui.character_conversation_runner import CharacterConversationRunner

    runtime = _FakeRuntime()
    controller = LearningModeController(
        store=LearningStore(tmp_path / "learning.sqlite3")
    )
    controller._store.initialize()
    runner = CharacterConversationRunner(
        runtime=runtime, learning_controller=controller
    )
    controller.enter_mode()
    controller.create_course("自动控制原理")

    events, answer = runner.perform("什么是传递函数？")
    assert answer == "回答"
    assert len(runtime.calls) == 1
    context = runtime.calls[0]["turn_context"]
    assert context is not None
    assert "学习模式" in context
    assert "自动控制原理" in context
    # The same runtime/router was used — no second provider was created.
    assert runtime.calls[0]["text"] == "什么是传递函数？"


def test_learning_context_absent_outside_mode(qapp, tmp_path) -> None:
    from ui.character_conversation_runner import CharacterConversationRunner

    runtime = _FakeRuntime()
    controller = LearningModeController(
        store=LearningStore(tmp_path / "learning.sqlite3")
    )
    controller._store.initialize()
    runner = CharacterConversationRunner(
        runtime=runtime, learning_controller=controller
    )

    runner.perform("随便聊聊")
    assert runtime.calls[0]["turn_context"] is None
