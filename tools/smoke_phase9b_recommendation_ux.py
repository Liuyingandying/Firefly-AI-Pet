"""Phase 9B desktop smoke — three required states, no online model calls.

Runs the real VisualShell offscreen and drives the exact three smoke inputs
(section 31), asserting the UI state each produces. QuickAskRunner.ask is
patched so no CLI is launched.
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


def _state_a(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("为什么这里报错？")
        assert ask_mock.call_count == 1, "A must go straight into Claude Short Talk"
        assert ask_mock.call_args[0][0] == "claude"
        assert not shell.recommendation_card.has_pending, "A must not show a card"
        assert shell.short_ask.running
    print("Smoke A  '为什么这里报错？'  -> Claude Short Talk (no card)     PASS")


def _state_b(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.short_ask.reset()
        shell.recommendation_card.clear_pending()
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("把这个模块重构一下")
        assert ask_mock.call_count == 0, "B must not auto-run anything"
        assert shell.recommendation_card.isVisible()
        assert shell.recommendation_card.pending_agent == "codex"
        assert shell.recommendation_card._status.text() == "Recommended · Codex"
    print("Smoke B  '把这个模块重构一下'  -> Recommended · Codex            PASS")


def _state_c(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.short_ask.reset()
        shell.recommendation_card.clear_pending()
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("分析这张图片")
        assert ask_mock.call_count == 0, "C must not reach any backend"
        assert shell.recommendation_card.isVisible()
        assert "Vision isn't available here yet" in shell.recommendation_card._title.text()
    print("Smoke C  '分析这张图片'  -> vision unavailable                   PASS")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    shell = VisualShell(None)
    try:
        _state_a(shell)
        _state_b(shell)
        _state_c(shell)
    finally:
        shell.shutdown()
    print("Phase 9B smoke passed — 3/3 states confirmed, zero online calls.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
