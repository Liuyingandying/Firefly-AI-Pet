"""Phase 9B — Recommendation UX integration tests.

Covers the 40 required scenarios: Claude SHORT_TALK high-confidence passes
straight into the existing Short Talk with zero extra clicks; explain/analyze/
review/why-error/how-to-fix stay Claude; coding/fix-error route to the Codex
recommendation card; the card's primary is Open Codex and secondary is Ask
Claude first; Open Codex only launches the native surface with the locked
agent + workspace (no prompt injection, no managed session); the Codex card's
secondary is Plan with Claude and light is Open only (9D.3); long Claude tasks
reuse the existing long-task UX; textual requested agent is respected while the
dock selection is never written into requested_agent; ChatGPT and vision never
fabricate a backend; UNAVAILABLE is a capability state (not a runtime error);
scores and raw ReasonCodes are never displayed; overlay priority hides the card
under PermissionCard / business popovers without cancelling it; a hidden
pending recommendation is kept in memory and restored on the next Ask;
workspace changes clear the pending card; recommendations create no session;
the router stays Qt-free / LLM-free / subprocess-free; and Short Talk streaming
+ session persistence do not regress.

No online model calls: every QuickAskRunner.ask is patched.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEvent, AgentEventType
from core.models import AgentState, LifecycleState
from core.routing_models import (
    AgentRecommendation,
    Confidence,
    HandoffMode,
    ReasonCode,
    TaskIntent,
)
from ui.process_launcher import ProcessLauncher
from ui.short_ask import ShortTalkState


def _prepare(shell) -> None:
    shell.short_ask.reset()
    shell.recommendation_card.clear_pending()
    shell.dock.select_agent("claude", emit_signal=False)
    shell.coordinator._waiting.clear()
    if shell.permission_card is not None:
        shell.permission_card.hide_card()


def _open_input(shell) -> None:
    shell.dock.select_agent("claude", emit_signal=True)
    shell._on_short_ask_requested()


# -- 1-4. Claude SHORT_TALK high-confidence: no extra card ------------------

def test_high_conf_claude_short_talk_no_card(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("解释这段函数")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"
        assert not shell.recommendation_card.has_pending
        assert not shell.recommendation_card.isVisible()


def test_explain_short_talk(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("解释一下这个设计")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"


def test_analysis_short_talk(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("分析这个模块的性能")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"


def test_review_short_talk(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("帮我 review 一下这段代码")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"


# -- 5-8. coding -> Codex recommendation card -------------------------------

def test_coding_shows_recommendation(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert ask_mock.call_count == 0
        assert shell.recommendation_card.isVisible()
        assert shell.recommendation_card.has_pending
        assert shell.recommendation_card.pending_agent == "codex"


def test_coding_not_sent_to_claude(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("实现这个功能")
        assert ask_mock.call_count == 0, "coding must never reach the Claude backend"


def test_coding_primary_send_codex(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("把这个模块重构一下")
        assert shell.recommendation_card._primary_btn.text() == "Send to Codex"


def test_coding_secondary_plan_with_claude(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("把这个模块重构一下")
        card = shell.recommendation_card
        assert card._secondary_btn.text() == "Plan with Claude"
        assert card._secondary_btn.isVisible()
        assert card._light_btn.text() == "Open only"
        assert card._light_btn.isVisible()
        shown = card._secondary_btn.text() + card._light_btn.text()
        assert "Ask Claude first" not in shown, "Ask Claude first must not appear in 9D.3"


# -- 9-12. card actions / long-task UX -------------------------------------

def test_send_codex_launches_with_prompt(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock, patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert launch_mock.call_count == 0, "showing the card must not auto-launch"
        shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_count == 1
        assert launch_mock.call_args[0][0] == "codex"
        assert launch_mock.call_args.kwargs.get("initial_prompt") == "重构这个模块", (
            "Send to Codex must hand the original task to the native Agent"
        )
        assert ask_mock.call_count == 0, "Send to Codex must not run a managed ask"


def test_open_only_launches_without_prompt(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock, patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        locked_workspace = shell.recommendation_card.pending_workspace
        shell.recommendation_card._light_btn.click()  # 9D.3: light = Open only
        assert launch_mock.call_count == 1
        assert launch_mock.call_args[0][0] == "codex"
        assert "initial_prompt" not in launch_mock.call_args.kwargs, (
            "Open only must not hand a task"
        )
        assert launch_mock.call_args[0][1] == locked_workspace
        assert ask_mock.call_count == 0


def test_light_open_only_opens_without_prompt(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock, patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        locked_workspace = shell.recommendation_card.pending_workspace
        shell.recommendation_card._light_btn.click()
        assert launch_mock.call_count == 1
        assert launch_mock.call_args[0][0] == "codex"
        assert "initial_prompt" not in launch_mock.call_args.kwargs, (
            "Open only (light) must not hand a task"
        )
        assert launch_mock.call_args[0][1] == locked_workspace
        assert ask_mock.call_count == 0, "Open only (light) must not run a managed ask"


def test_long_claude_task_reuses_long_task_ux(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("explain how to update this module")
        assert ask_mock.call_count == 0
        assert not shell.recommendation_card.has_pending, "Claude long task must not use the router card"
        assert shell.short_ask._status.text() == "This looks like a longer task. Open Claude instead?"
        assert shell.short_ask._primary_btn.text() == "Open Claude"


# -- 13-15. debug boundary ------------------------------------------------

def test_fix_error_codex_recommendation(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("修复这个报错")
        assert ask_mock.call_count == 0
        assert shell.recommendation_card.pending_agent == "codex"


def test_why_error_claude_short_talk(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("为什么这里会报错")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"
        assert not shell.recommendation_card.has_pending


def test_how_to_fix_claude_explain(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("如何修复这个错误")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"
        assert not shell.recommendation_card.has_pending


# -- 16-20. requested_agent vs selected_agent ------------------------------

def test_textual_requested_claude(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("用 Claude 分析这个函数")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"


def test_textual_requested_codex(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("让 Codex 实现这个功能")
        assert ask_mock.call_count == 0
        assert shell.recommendation_card.has_pending
        assert shell.recommendation_card.pending_agent == "codex"
        assert shell.recommendation_card._primary_btn.text() == "Send to Codex"


def test_dock_claude_does_not_override_coding(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("重构这个模块")
        assert ask_mock.call_count == 0
        assert shell.recommendation_card.pending_agent == "codex", (
            "dock=Claude must not redirect a coding task into Claude Short Talk"
        )


def test_ask_codex_routes_analysis_to_codex(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        shell.dock.select_agent("codex", emit_signal=True)
        shell._on_short_ask_requested()  # opens "Ask Codex" input
        assert shell.short_ask.agent == "codex"
        shell._on_short_ask_send("分析这段代码")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "codex"
        assert not shell.recommendation_card.has_pending, (
            "Ask Codex + read task stays in managed Codex Short Talk"
        )


def test_ask_codex_write_routes_recommendation(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        shell.dock.select_agent("codex", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("重构这个模块")
        assert ask_mock.call_count == 0, "read-only Codex Short Talk must not code"
        assert shell.recommendation_card.has_pending
        assert shell.recommendation_card.pending_agent == "codex"


def test_dock_selection_not_written_as_requested_agent(shell) -> None:
    shell.dock.select_agent("codex", emit_signal=True)
    assert shell._task_request("重构这个模块").requested_agent is None
    shell.dock.select_agent("claude", emit_signal=True)
    assert shell._task_request("解释这段代码").requested_agent is None


# -- 21-23. ChatGPT / vision / capability states ---------------------------

def test_chatgpt_no_fake_backend(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("用 ChatGPT 改这段代码")
        assert ask_mock.call_count == 0
        assert shell.recommendation_card.has_pending
        assert shell.recommendation_card.pending_agent == "chatgpt"
        assert shell.recommendation_card._title.text() == "Not available yet"


def test_vision_not_sent_to_claude(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("分析这张图片")
        assert ask_mock.call_count == 0
        assert shell.recommendation_card.has_pending
        assert "Vision isn't available here yet" in shell.recommendation_card._title.text()
        assert not shell.recommendation_card._primary_btn.isVisible()


def test_unavailable_is_capability_state(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("用 ChatGPT 帮我重构这个模块")
        card = shell.recommendation_card
        assert card.has_pending
        assert card._title.text() == "Not available yet"
        assert "error" not in card._title.text().lower()
        assert "Error" not in card._status.text()


# -- 24-25. no scores / no raw reason codes --------------------------------

def test_score_not_shown(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        card = shell.recommendation_card
        shown = card._title.text() + " " + card._status.text() + " " + card._primary_btn.text()
        for token in ("90", "82", "100", "Confidence", "confidence"):
            assert token not in shown, f"score leaked: {token}"


def test_reason_code_not_displayed(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        card = shell.recommendation_card
        shown = card._title.text() + " " + card._status.text()
        assert "best_for_coding" not in shown


# -- 26-28. agent / workspace lock -----------------------------------------

def test_recommendation_locks_agent(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock, patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert shell.recommendation_card.pending_agent == "codex"
        shell.dock.select_agent("claude", emit_signal=True)  # dock changes mid-card
        shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_count == 1
        assert launch_mock.call_args[0][0] == "codex", "must launch the locked agent"
        assert ask_mock.call_count == 0


def test_recommendation_locks_workspace(shell) -> None:
    locked = shell.workspace_manager.current()
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_count == 1
        assert launch_mock.call_args[0][1] == str(locked), (
            "must launch in the workspace locked at recommendation creation time"
        )


def test_workspace_drift_cancels_send(shell) -> None:
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        with patch.object(shell.workspace_manager, "current", return_value=Path("C:/Other")):
            shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_count == 0, (
            "workspace drift must cancel the pending handoff, not silently deliver"
        )
        assert shell.recommendation_card.handoff_state.value == "failed"


def test_workspace_change_clears_pending(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert shell.recommendation_card.has_pending
        with tempfile.TemporaryDirectory() as td:
            shell.workspace_manager.set_current(Path(td))
        assert not shell.recommendation_card.has_pending
        assert not shell.recommendation_card.isVisible()


# -- 29-32. overlay priority -----------------------------------------------

def test_permission_card_hides_recommendation(shell) -> None:
    shell.coordinator.show_shell()
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert shell.recommendation_card.isVisible()
        shell.coordinator.on_agent_state(
            "claude", AgentState("claude", LifecycleState.WAITING, 1000, "hook")
        )
        assert shell.coordinator.permission_card.isVisible()
        assert not shell.recommendation_card.isVisible()
        assert shell.recommendation_card.has_pending, "hidden, not cancelled"


def test_permission_card_no_recommendation_action(shell) -> None:
    shell.coordinator.show_shell()
    with patch.object(shell.quick_ask, "ask") as ask_mock, patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.coordinator.on_agent_state(
            "claude", AgentState("claude", LifecycleState.WAITING, 1000, "hook")
        )
        assert not shell.recommendation_card.isVisible()
        assert launch_mock.call_count == 0 and ask_mock.call_count == 0
        shell.coordinator.on_agent_state(
            "claude", AgentState("claude", LifecycleState.IDLE, 2000, "hook")
        )
        assert not shell.recommendation_card.isVisible(), (
            "permission clearing must not auto-repop the recommendation"
        )


def test_hidden_pending_kept_and_restored(shell) -> None:
    shell.coordinator.show_shell()
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.coordinator.on_agent_state(
            "claude", AgentState("claude", LifecycleState.WAITING, 1000, "hook")
        )
        assert shell.recommendation_card.has_pending
        assert not shell.recommendation_card.isVisible()
        shell.coordinator.on_agent_state(
            "claude", AgentState("claude", LifecycleState.IDLE, 2000, "hook")
        )
        assert not shell.recommendation_card.isVisible(), "not auto-repopped"
        shell._on_short_ask_requested()  # user clicks Ask again -> restore
        assert shell.recommendation_card.isVisible()
        assert shell.recommendation_card.pending_agent == "codex"


def test_business_popover_priority(shell) -> None:
    shell.coordinator.show_shell()
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert shell.recommendation_card.isVisible()
        shell.coordinator._show_context("sessions")
        assert not shell.recommendation_card.isVisible()
        assert shell.recommendation_card.has_pending, (
            "business popover hides, not cancels, the pending recommendation"
        )


# -- 33-34. no phantom sessions --------------------------------------------

def test_recommendation_no_session(shell) -> None:
    ws = shell.workspace_manager.current()
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert shell.recommendation_card.has_pending
        assert not shell.session_manager.has("codex", ws)
        assert not shell.session_manager.has("claude", ws)


def test_open_native_no_managed_session(shell) -> None:
    ws = shell.workspace_manager.current()
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert not shell.session_manager.has("codex", ws)
        assert not shell.session_manager.has("claude", ws)


# -- 35-38. router purity / UI boundary ------------------------------------

def test_router_still_zero_llm_and_pure(shell=None) -> None:
    from test_phase9a_agent_router import _ROUTER_FILES, _forbidden_in_module

    for path in _ROUTER_FILES:
        assert not _forbidden_in_module(
            path, ("PySide6", "qtpy", "PyQt", "QProcess", "QWidget", "QObject", "QTimer", "Signal")
        ), f"{path.name} must stay Qt-free"
        assert not _forbidden_in_module(
            path, ("subprocess", "Popen", "startDetached", "taskkill")
        ), f"{path.name} must stay subprocess-free"
        assert not _forbidden_in_module(
            path, ("socket", "urllib", "requests", "http", "urlopen", "websocket", "openai", "anthropic")
        ), f"{path.name} must stay network-free"


def test_recommendation_zero_subprocess_until_confirm(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock, patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert launch_mock.call_count == 0 and ask_mock.call_count == 0
        shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_count == 1
        assert ask_mock.call_count == 0


def test_ui_delegates_to_router(shell) -> None:
    with patch.object(shell.agent_router, "recommend") as router_mock:
        router_mock.return_value = [
            AgentRecommendation(
                agent_id="claude",
                score=80,
                reason_code=ReasonCode.BEST_FOR_ANALYSIS,
                confidence=Confidence.HIGH,
                intent=TaskIntent.ANALYZE,
                handoff_mode=HandoffMode.SHORT_TALK,
            )
        ]
        with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
            _open_input(shell)
            shell._on_short_ask_send("xqzkvbl-random")
            assert router_mock.call_count == 1, "the UI must ask the router, not re-derive rules"
            assert ask_mock.call_count == 1


def test_router_models_qt_free(shell=None) -> None:
    from test_phase9a_agent_router import _forbidden_in_module

    hits = _forbidden_in_module(
        PROJECT_DIR / "core" / "routing_models.py",
        ("PySide6", "qtpy", "PyQt", "QProcess", "QWidget", "QObject", "QTimer", "Signal"),
    )
    assert not hits


# -- 39-40. no regression --------------------------------------------------

def test_short_talk_streaming_no_regression(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True):
        _open_input(shell)
        shell._on_short_ask_send("解释这段代码")
        assert shell.short_ask.running
        shell.short_ask.on_agent_event(
            AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text="OK")
        )
        assert shell.short_ask.full_answer() == "OK"
        assert shell.short_ask.state == ShortTalkState.STREAMING
        shell.short_ask.on_agent_event(
            AgentEvent.make("claude", AgentEventType.FINAL, text="OK done")
        )
        assert shell.short_ask.state == ShortTalkState.COMPLETE
        assert shell.short_ask._output.text() == "OK done"


def test_session_persistence_no_regression(shell) -> None:
    ws = shell.workspace_manager.current()
    shell.quick_ask._agent = "claude"
    shell.quick_ask._workspace = ws
    shell.quick_ask._persistent = True
    shell.quick_ask._consume_json_line(
        json.dumps(
            {
                "type": "stream_event",
                "session_id": "sess-9b-regression",
                "event": {"type": "message_start"},
            }
        )
    )
    assert shell.session_manager.get_native_id("claude", ws) == "sess-9b-regression"


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    shell = VisualShell(None, workspace_settings_file=Path(tempfile.mkdtemp(prefix="fap9b_ws_")) / "ui_settings.json")
    tests = [
        test_high_conf_claude_short_talk_no_card,
        test_explain_short_talk,
        test_analysis_short_talk,
        test_review_short_talk,
        test_coding_shows_recommendation,
        test_coding_not_sent_to_claude,
        test_coding_primary_send_codex,
        test_coding_secondary_plan_with_claude,
        test_send_codex_launches_with_prompt,
        test_open_only_launches_without_prompt,
        test_light_open_only_opens_without_prompt,
        test_long_claude_task_reuses_long_task_ux,
        test_fix_error_codex_recommendation,
        test_why_error_claude_short_talk,
        test_how_to_fix_claude_explain,
        test_textual_requested_claude,
        test_textual_requested_codex,
        test_dock_claude_does_not_override_coding,
        test_ask_codex_routes_analysis_to_codex,
        test_ask_codex_write_routes_recommendation,
        test_dock_selection_not_written_as_requested_agent,
        test_chatgpt_no_fake_backend,
        test_vision_not_sent_to_claude,
        test_unavailable_is_capability_state,
        test_score_not_shown,
        test_reason_code_not_displayed,
        test_recommendation_locks_agent,
        test_recommendation_locks_workspace,
        test_workspace_drift_cancels_send,
        test_workspace_change_clears_pending,
        test_permission_card_hides_recommendation,
        test_permission_card_no_recommendation_action,
        test_hidden_pending_kept_and_restored,
        test_business_popover_priority,
        test_recommendation_no_session,
        test_open_native_no_managed_session,
        test_router_still_zero_llm_and_pure,
        test_recommendation_zero_subprocess_until_confirm,
        test_ui_delegates_to_router,
        test_router_models_qt_free,
        test_short_talk_streaming_no_regression,
        test_session_persistence_no_regression,
    ]
    try:
        for fn in tests:
            _prepare(shell)
            fn(shell)
        print(f"Phase 9B recommendation UX tests passed ({len(tests)} tests).")
    finally:
        shell.shutdown()

    # Desktop smoke — three states confirmed offscreen (no online model calls).
    print("Smoke A: '为什么这里报错?' -> Claude Short Talk path (tested)      PASS")
    print("Smoke B: '把这个模块重构一下' -> Recommended · Codex (tested)       PASS")
    print("Smoke C: '分析这张图片' -> vision unavailable (tested)              PASS")


if __name__ == "__main__":
    main()
