"""Ask button -> existing Companion chat window.

Wiring under test:
    AskPill.ask_clicked  ->  coordinator.short_ask_requested
        -> VisualShell._on_short_ask_requested
        -> CompanionChatWindow.open_singleton(runner=character_conversation)

Guarantees pinned here: single instance, raise-not-duplicate, input focus
requested, close -> reopen, zero screen-vision captures, zero provider calls.
"""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt, QObject, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def clean_singleton():
    from ui.companion_chat_window import CompanionChatWindow

    yield
    window = CompanionChatWindow._instance
    if window is not None:
        window.close()
        window.deleteLater()
    CompanionChatWindow._instance = None


class FakeRunner(QObject):
    """Duck-typed CharacterConversationRunner (no runtime, no provider)."""

    agent_event = Signal(object)

    def __init__(self):
        super().__init__()
        self.asked = []

    @property
    def history(self):
        return []

    @property
    def running(self):
        return False

    def ask(self, text):
        self.asked.append(text)


# ------------------------------------------------------------ AskPill


def test_ask_pill_click_emits_ask_clicked(qapp):
    from PySide6.QtWidgets import QWidget

    from ui.short_ask import AskPill

    pill = AskPill()
    fired = []
    pill.ask_clicked.connect(lambda: fired.append(1))
    event = QMouseEvent(
        QEvent.MouseButtonRelease,
        QPointF(10, 10),
        QPointF(10, 10),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    pill.mouseReleaseEvent(event)
    assert fired == [1]


def test_app_handler_opens_companion_chat_singleton():
    """The VisualShell Ask handler must route to the CompanionChatWindow
    singleton (source-level pin: the handler body is load-bearing)."""
    import inspect

    from app import VisualShell

    source = inspect.getsource(VisualShell._on_short_ask_requested)
    assert "CompanionChatWindow.open_singleton" in source
    assert "permission_card" not in source  # approval card must never block Ask


# --------------------------------------- open_singleton lifecycle (A-D)


def test_a_ask_opens_companion_chat(qapp):
    from ui.companion_chat_window import CompanionChatWindow

    runner = FakeRunner()
    window = CompanionChatWindow.open_singleton(runner=runner)
    assert window.isVisible()
    assert window.runner is runner  # the shell's runner is reused, not duplicated
    assert CompanionChatWindow._instance is window


def test_second_ask_reuses_same_instance(qapp):
    from ui.companion_chat_window import CompanionChatWindow

    runner = FakeRunner()
    first = CompanionChatWindow.open_singleton(runner=runner)
    for _ in range(4):  # five Ask clicks total -> still one instance
        again = CompanionChatWindow.open_singleton(runner=runner)
        assert again is first
    assert CompanionChatWindow._instance is first


def test_close_then_ask_reopens(qapp):
    from ui.companion_chat_window import CompanionChatWindow

    runner = FakeRunner()
    first = CompanionChatWindow.open_singleton(runner=runner)
    first.close()
    assert CompanionChatWindow._instance is None

    second = CompanionChatWindow.open_singleton(runner=runner)
    assert second is not first
    assert second.isVisible()
    # the conversation runner (and its history/session) is still the same one
    assert second.runner is runner


def test_focus_requested_on_input(qapp, monkeypatch):
    from ui.companion_chat_window import CompanionChatWindow

    window = CompanionChatWindow(runner=FakeRunner())
    focused = []
    original_setFocus = window.input.setFocus

    def spy_setFocus():
        focused.append(1)
        original_setFocus()

    monkeypatch.setattr(window.input, "setFocus", spy_setFocus)
    # register as the singleton and re-enter via the Ask entry point
    CompanionChatWindow._instance = window
    CompanionChatWindow.open_singleton(runner=window.runner)
    assert focused == [1]


# ------------------------------------- E: no vision, no provider calls


def test_ask_triggers_zero_looks(qapp, monkeypatch):
    from core.screen_vision.service import ScreenVisionService
    from ui.companion_chat_window import CompanionChatWindow

    def forbid(self, *args, **kwargs):
        raise AssertionError("Ask triggered a screen-vision look")

    monkeypatch.setattr(ScreenVisionService, "look", forbid)
    CompanionChatWindow.open_singleton(runner=FakeRunner())


def test_ask_calls_zero_providers(qapp, monkeypatch):
    from providers.base import OpenAICompatibleProvider
    from ui.companion_chat_window import CompanionChatWindow

    def forbid(self, *args, **kwargs):
        raise AssertionError("Ask called an AI provider")

    monkeypatch.setattr(OpenAICompatibleProvider, "chat", forbid)
    CompanionChatWindow.open_singleton(runner=FakeRunner())


def test_ask_sends_nothing_automatically(qapp):
    from ui.companion_chat_window import CompanionChatWindow

    runner = FakeRunner()
    window = CompanionChatWindow.open_singleton(runner=runner)
    assert runner.asked == []  # opening Ask never fills or sends a message
    assert window.input.text() == ""
