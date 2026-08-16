"""ChatGPT dock -> system default browser launch — offline tests.

No real browser opens. Verifies the ChatGPT dock entry routes straight to
QDesktopServices.openUrl("https://chatgpt.com/") while Claude/Codex dock clicks
keep their selection-only behavior and never touch Quick Ask / sessions / router.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication


def test_chatgpt_dock_click_opens_url(shell) -> None:
    with patch("ui.process_launcher.QDesktopServices.openUrl", return_value=True) as open_mock:
        shell.dock.select_agent("chatgpt", emit_signal=True)
    assert open_mock.call_count == 1
    url = open_mock.call_args[0][0]
    assert url.toString() == "https://chatgpt.com/"


def test_claude_dock_click_no_browser(shell) -> None:
    with patch("ui.process_launcher.QDesktopServices.openUrl") as open_mock:
        shell.dock.select_agent("claude", emit_signal=True)
    assert open_mock.call_count == 0


def test_codex_dock_click_no_browser(shell) -> None:
    with patch("ui.process_launcher.QDesktopServices.openUrl") as open_mock:
        shell.dock.select_agent("codex", emit_signal=True)
    assert open_mock.call_count == 0


def test_chatgpt_dock_click_no_backend(shell) -> None:
    with patch("ui.process_launcher.QDesktopServices.openUrl", return_value=True), patch.object(
        shell.quick_ask, "ask"
    ) as ask_mock, patch.object(shell.agent_router, "recommend") as router_mock:
        shell.dock.select_agent("chatgpt", emit_signal=True)
    assert ask_mock.call_count == 0
    assert router_mock.call_count == 0
    assert shell.session_manager.has("chatgpt", shell.workspace_manager.current()) is False


def test_chatgpt_dock_click_open_failure_shows_notice(shell) -> None:
    with patch("ui.process_launcher.QDesktopServices.openUrl", return_value=False), patch.object(
        shell.bubble, "show_message"
    ) as show_mock:
        shell.dock.select_agent("chatgpt", emit_signal=True)
    assert show_mock.call_count == 1
    assert show_mock.call_args[0][1] == "Couldn't open ChatGPT."


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    shell = VisualShell(None)
    try:
        test_chatgpt_dock_click_opens_url(shell)
        test_claude_dock_click_no_browser(shell)
        test_codex_dock_click_no_browser(shell)
        test_chatgpt_dock_click_no_backend(shell)
        test_chatgpt_dock_click_open_failure_shows_notice(shell)
        print("ChatGPT web launch tests passed.")
    finally:
        shell.shutdown()


if __name__ == "__main__":
    main()
