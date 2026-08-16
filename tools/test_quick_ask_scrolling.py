"""Quick Ask / Ask Claude — answer scrolling regression tests.

Offline only: no Claude, no Provider, no Codex. Verifies that the Short Ask
answer viewport scrolls long responses instead of clipping them, and that the
follow-tail auto-scroll pauses on an upward scroll and resumes on return to the
bottom. Footer controls stay fixed outside the scrollable viewport.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEvent, AgentEventType
from ui.short_ask import MAX_ANSWER_HEIGHT, ShortAskPanel


def _delta(panel: ShortAskPanel, text: str) -> None:
    panel.on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text=text))


def _streaming_panel(app: QApplication) -> ShortAskPanel:
    panel = ShortAskPanel()
    panel.show_input("claude")
    panel.set_running("Connecting…")
    panel.show()
    app.processEvents()
    return panel


# A. Short text needs no scrolling and stays compact.
def test_short_text_needs_no_scroll(app: QApplication) -> None:
    panel = _streaming_panel(app)
    panel.add_answer("Short and sweet.")
    app.processEvents()
    bar = panel._output.verticalScrollBar()
    assert panel._output.text() == "Short and sweet."
    assert bar.maximum() == bar.minimum() == 0
    assert panel._output.height() < MAX_ANSWER_HEIGHT, "short answer stays compact"
    panel.close()


# B. Long text scrolls, can reach the tail, and is never clipped.
def test_long_text_scrolls_to_tail(app: QApplication) -> None:
    panel = _streaming_panel(app)
    long_text = "word " * 2000
    panel.set_answer(long_text)
    app.processEvents()
    bar = panel._output.verticalScrollBar()
    assert bar.maximum() > bar.minimum(), "long answer needs a scrollbar"
    assert panel._output.height() == MAX_ANSWER_HEIGHT
    assert panel._output.text() == long_text, "full answer is rendered, not clipped"
    panel._output.scroll_to_bottom()
    app.processEvents()
    assert bar.value() == bar.maximum(), "can scroll to the very bottom"
    panel.close()


# C. Streaming follows the tail: new text keeps the view pinned to the bottom.
def test_streaming_follows_tail(app: QApplication) -> None:
    panel = _streaming_panel(app)
    for i in range(40):
        _delta(panel, f"line {i} of the answer\n")
    app.processEvents()
    bar = panel._output.verticalScrollBar()
    assert panel._output.follow_tail is True
    assert bar.value() == bar.maximum(), "view starts pinned to the bottom"
    _delta(panel, "tail token\n")
    app.processEvents()
    assert panel._output.follow_tail is True
    assert bar.value() == bar.maximum(), "new text keeps the view at the bottom"
    panel.close()


# D. Scrolling up pauses following; new text must not yank the user down.
def test_scroll_up_pauses_follow(app: QApplication) -> None:
    panel = _streaming_panel(app)
    for i in range(40):
        _delta(panel, f"line {i} of the answer\n")
    app.processEvents()
    bar = panel._output.verticalScrollBar()
    bar.setValue(0)  # user scrolls up to read history
    app.processEvents()
    assert panel._output.follow_tail is False
    _delta(panel, "more text\n" * 10)
    app.processEvents()
    assert bar.value() < bar.maximum(), "must not yank the user back to the bottom"
    assert panel._output.follow_tail is False
    panel.close()


# E. Returning to the bottom resumes follow-tail.
def test_scroll_back_resumes_follow(app: QApplication) -> None:
    panel = _streaming_panel(app)
    for i in range(40):
        _delta(panel, f"line {i} of the answer\n")
    app.processEvents()
    bar = panel._output.verticalScrollBar()
    bar.setValue(0)
    app.processEvents()
    assert panel._output.follow_tail is False
    bar.setValue(bar.maximum())  # user returns to the bottom
    app.processEvents()
    assert panel._output.follow_tail is True
    _delta(panel, "resumed tail\n")
    app.processEvents()
    assert panel._output.follow_tail is True
    assert bar.value() == bar.maximum()
    panel.close()


# F. Footer controls stay fixed below the scrollable answer.
def test_footer_fixed_below_answer(app: QApplication) -> None:
    panel = _streaming_panel(app)
    assert panel._primary_btn.isVisible()
    assert panel._primary_btn.text() == "Stop"
    assert not panel._output.isAncestorOf(panel._primary_btn), "footer is outside the viewport"
    for i in range(30):
        _delta(panel, f"line {i} of the answer\n")
    app.processEvents()
    assert panel._output.geometry().bottom() <= panel._primary_btn.geometry().top()
    panel.close()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    test_short_text_needs_no_scroll(app)
    test_long_text_scrolls_to_tail(app)
    test_streaming_follows_tail(app)
    test_scroll_up_pauses_follow(app)
    test_scroll_back_resumes_follow(app)
    test_footer_fixed_below_answer(app)
    print("Quick Ask scrolling tests passed.")


if __name__ == "__main__":
    main()
