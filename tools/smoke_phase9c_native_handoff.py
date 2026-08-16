"""Phase 9C desktop smoke — Send to Codex / Plan with Claude / Open only.

Runs the real VisualShell offscreen and drives the smoke input (section 36):
"把这个模块重构一下" must render Recommended · Codex with the three actions and
a mocked launch / mocked workflow executor (no online model calls, no real CLI
spawn).
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

from core.agent_events import AgentEvent, AgentEventType
from core.handoff import HandoffState
from core.workflow_models import WorkflowStepState
from ui.process_launcher import ProcessLauncher


def main() -> int:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    shell = VisualShell(None)
    try:
        with patch.object(shell.quick_ask, "ask"), patch.object(
            ProcessLauncher, "launch_agent", return_value=(True, "ok")
        ) as launch_mock:
            shell.short_ask.reset()
            shell.recommendation_card.clear_pending()
            shell.dock.select_agent("claude", emit_signal=True)
            shell._on_short_ask_requested()
            shell._on_short_ask_send("把这个模块重构一下")
            card = shell.recommendation_card
            assert card.isVisible() and card.has_pending
            assert card.pending_agent == "codex"
            assert card._status.text() == "Recommended · Codex"
            assert card._primary_btn.text() == "Send to Codex"
            assert card._secondary_btn.text() == "Plan with Claude"
            assert card._light_btn.text() == "Open only"
            assert launch_mock.call_count == 0, "card show must not auto-launch"
            print("Smoke 1  '把这个模块重构一下' -> Recommended · Codex + Send/Plan/Open   PASS")

            card._primary_btn.click()
            assert launch_mock.call_count == 1, "Send click must launch exactly once"
            assert launch_mock.call_args.kwargs.get("initial_prompt") == "把这个模块重构一下"
            assert card.handoff_state == HandoffState.HANDED_OFF
            assert card._status.text() == "Sent to Codex"
            print("Smoke 2  Send to Codex -> HANDED_OFF, original prompt handed over   PASS")

            card.clear_pending()
            shell.dock.select_agent("claude", emit_signal=True)
            shell._on_short_ask_requested()
            shell._on_short_ask_send("把这个模块重构一下")
            card._light_btn.click()
            assert launch_mock.call_count == 2, "Open only must call the launcher"
            assert "initial_prompt" not in launch_mock.call_args.kwargs, (
                "Open only must not hand a task"
            )
            print("Smoke 3  Open only -> launcher, no prompt, no handoff             PASS")

            card.clear_pending()
            shell.dock.select_agent("claude", emit_signal=True)
            shell._on_short_ask_requested()
            shell._on_short_ask_send("把这个模块重构一下")
            with patch.object(shell.plan_executor._runner, "ask", return_value=True):
                card._secondary_btn.click()
            wf = shell.workflow_card.plan
            assert wf is not None
            assert wf.steps[0].state == WorkflowStepState.RUNNING
            assert shell.workflow_card.isVisible()
            assert shell.workflow_card._rows[0]._status.text() == "Planning…"
            print("Smoke 4  Plan with Claude -> WorkflowCard, Step1 RUNNING/Planning…   PASS")
    finally:
        shell.shutdown()
    print("Phase 9C smoke passed — 4/4 states confirmed, zero online calls.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
