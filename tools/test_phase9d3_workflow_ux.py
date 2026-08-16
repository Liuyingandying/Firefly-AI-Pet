"""Phase 9D.3 — Workflow UX + Plan Step Integration tests.

Covers the 56 required scenarios: the Codex coding card shows Send to Codex /
Plan with Claude / Open only (no Ask Claude first); Send to Codex keeps the 9C
contract and creates no workflow; Open only creates no workflow; only Plan with
Claude creates a PLAN_IMPLEMENT_REVIEW workflow; the original TaskRequest +
workspace stay locked; workspace drift blocks workflow creation; a second
workflow is rejected while one is active; the Plan click is the explicit Step-1
confirmation with confirm and execute as two separate calls; the WorkflowCard
shows immediately with Step 1 status and is driven only by WorkflowEvent
(never provider raw events, never AgentEvent); artifact events never leak
internal ids; Plan success -> Step 1 "Plan ready", Step 2 "Waiting for
confirmation", Step 3 "Pending"; Step 2 is never auto-confirmed or started;
Codex is never started (online 0); Open plan uses an ArtifactStore-validated
path and arbitrary paths cannot be opened; close / business popover /
PermissionCard hide-not-cancel; a hidden workflow keeps receiving state and is
restorable via a minimal entry; Plan ERROR/CANCELLED render failed/cancelled
UI; explicit cancel routes to the executor while running and to the
coordinator while waiting; no auto-retry / no fallback agent; SessionManager,
sessions.json, and external lifecycle are untouched; plan.md exists in full; the
UI never renders the full plan or UUIDs; Light Glass style; RecommendationCard /
ShortTalk / 9C handoff regressions; coordinator stays Qt-free; no workflow
persistence / changed-files / review / automatic Step 2 execution.

No online model calls: QuickAskRunner.ask and the PlanStepExecutor runner are
mocked throughout.
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
from core.routing_models import TaskRequest
from core.workflow_models import (
    ArtifactKind,
    WorkflowEventType,
    WorkflowState,
    WorkflowStepState,
)
from ui import theme
from ui.process_launcher import ProcessLauncher
from ui.popover_base import PopoverBase

from test_phase9a_agent_router import _forbidden_in_module

WORKFLOW_CARD_FILE = PROJECT_DIR / "ui" / "workflow_card.py"
EXECUTOR_FILE = PROJECT_DIR / "ui" / "workflow_executor.py"
COORDINATOR_FILE = PROJECT_DIR / "core" / "workflow_coordinator.py"

PLAN_TEXT = (
    "# Implementation Plan\n\n"
    "- Goal: refactor the auth module into smaller functions\n"
    "- Files: auth.py\n"
    "- Changes: extract validation, split login flow\n"
    "- Tests: add unit tests for each extracted function\n"
)


def _prepare(shell) -> None:
    shell.short_ask.reset()
    shell.recommendation_card.clear_pending()
    shell.dock.select_agent("claude", emit_signal=False)
    shell.coordinator._waiting.clear()
    if shell.permission_card is not None:
        shell.permission_card.hide_card()
    shell.workflow_card.hide_card()
    shell.workflow_card._workflow_id = None
    shell.workflow_card._plan = None
    shell.workflow_card._blocked_message = ""
    shell._active_workflow_id = None
    # Reset the shared executor so a test that ran a real execute() (without
    # driving it to completion) cannot leak ExecutionBusy into the next test.
    ex = shell.plan_executor
    ex._busy = False
    ex._plan = None
    ex._workflow_id = None
    ex._step_id = None
    ex._collected_deltas = []
    ex._final_text = ""
    ex._saw_text = False
    ex._saw_error = False
    ex._cancelled = False
    ex._completed = False


def _open_input(shell) -> None:
    shell.dock.select_agent("claude", emit_signal=True)
    shell._on_short_ask_requested()


def _show_coding_recommendation(shell, prompt: str = "把这个模块重构一下") -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send(prompt)
    assert shell.recommendation_card.has_pending
    assert shell.recommendation_card.pending_agent == "codex"


def _click_plan_with_claude(shell):
    with patch.object(shell.plan_executor._runner, "ask", return_value=True) as ask_mock:
        shell.recommendation_card._secondary_btn.click()
    return ask_mock


def _start_workflow(shell, prompt: str = "把这个模块重构一下"):
    _show_coding_recommendation(shell, prompt)
    _click_plan_with_claude(shell)
    wfid = shell.workflow_card.workflow_id
    assert wfid is not None, "Plan with Claude must create and track a workflow"
    return shell.workflow_coordinator.get_plan(wfid)


def _drive_plan_success(shell, text: str = PLAN_TEXT) -> None:
    ex = shell.plan_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=text))
    ex._on_finished(text, 0)


def _card_text(card) -> str:
    parts = [card._title.text(), card._status.text()]
    for row in getattr(card, "_rows", ()):
        parts.extend([row._agent.text(), row._title.text(), row._status.text()])
    for name in ("_primary_btn", "_secondary_btn", "_light_btn", "_open_btn", "_cancel_btn"):
        widget = getattr(card, name, None)
        if widget is not None:
            parts.append(widget.text())
    return " ".join(parts)


# -- 1-4. coding recommendation card layout -------------------------------

def test_coding_primary_send_codex(shell) -> None:
    _show_coding_recommendation(shell)
    assert shell.recommendation_card._primary_btn.text() == "Send to Codex"
    assert shell.recommendation_card._primary_btn.isVisible()


def test_coding_secondary_plan_with_claude(shell) -> None:
    _show_coding_recommendation(shell)
    assert shell.recommendation_card._secondary_btn.text() == "Plan with Claude"
    assert shell.recommendation_card._secondary_btn.isVisible()


def test_coding_light_open_only(shell) -> None:
    _show_coding_recommendation(shell)
    assert shell.recommendation_card._light_btn.text() == "Open only"
    assert shell.recommendation_card._light_btn.isVisible()


def test_ask_claude_first_not_shown(shell) -> None:
    _show_coding_recommendation(shell)
    card = shell.recommendation_card
    shown = _card_text(card) + card._light_btn.text() + card._secondary_btn.text()
    assert "Ask Claude first" not in shown, "Ask Claude first must not appear in 9D.3"


# -- 5-7. Send to Codex / Open only create no workflow ---------------------

def test_send_to_codex_keeps_9c_contract(shell) -> None:
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")) as launch_mock:
        _show_coding_recommendation(shell)
        prompt = shell.recommendation_card.pending_prompt
        shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_count == 1
        assert launch_mock.call_args.kwargs.get("initial_prompt") == prompt
        assert shell.recommendation_card.handoff is not None
        assert shell.recommendation_card.handoff_state.value == "handed_off"


def test_send_to_codex_no_workflow(shell) -> None:
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")):
        _show_coding_recommendation(shell)
        shell.recommendation_card._primary_btn.click()
    assert shell.workflow_card.workflow_id is None
    assert shell.workflow_coordinator._plans == {}


def test_open_only_no_workflow(shell) -> None:
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")):
        _show_coding_recommendation(shell)
        shell.recommendation_card._light_btn.click()
    assert shell.workflow_card.workflow_id is None
    assert shell.workflow_coordinator._plans == {}


# -- 8-10. workflow creation + locks --------------------------------------

def test_plan_with_claude_creates_workflow(shell) -> None:
    plan = _start_workflow(shell)
    assert plan is not None
    assert plan.kind.value == "plan_implement_review"
    assert len(plan.steps) == 3


def test_original_task_request_preserved(shell) -> None:
    prompt = "把这个模块重构一下"
    plan = _start_workflow(shell, prompt)
    assert plan.original_request.text == prompt
    assert isinstance(plan.original_request, TaskRequest)


def test_workspace_locked(shell) -> None:
    _show_coding_recommendation(shell)
    locked = shell.recommendation_card.pending_workspace
    _click_plan_with_claude(shell)
    plan = shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)
    assert plan.workspace == locked
    assert plan.original_request.workspace == locked


# -- 11-12. drift + one-active-workflow ------------------------------------

def test_workspace_drift_blocks_workflow(shell) -> None:
    _show_coding_recommendation(shell)
    before = len(shell.workflow_coordinator._plans)
    with patch.object(shell.workspace_manager, "current", return_value=Path("C:/Other")):
        with patch.object(shell.plan_executor._runner, "ask") as ask_mock:
            shell.recommendation_card._secondary_btn.click()
    assert ask_mock.call_count == 0, "drift must not execute a workflow step"
    assert len(shell.workflow_coordinator._plans) == before, "drift must not create a workflow"
    assert shell.workflow_card.workflow_id is None, "drift must not track a workflow"
    assert not shell.recommendation_card.has_pending, "drift must cancel the pending recommendation"


def test_second_workflow_rejected(shell) -> None:
    plan1 = _start_workflow(shell)
    wfid1 = plan1.workflow_id
    before = len(shell.workflow_coordinator._plans)
    _show_coding_recommendation(shell, "另一个重构任务")
    with patch.object(shell.plan_executor._runner, "ask") as ask_mock:
        shell.recommendation_card._secondary_btn.click()
    assert ask_mock.call_count == 0, "no second workflow may execute"
    assert len(shell.workflow_coordinator._plans) == before, "no second workflow may be created"
    assert shell.workflow_card.workflow_id == wfid1, "the card keeps tracking the active workflow"
    assert shell.workflow_card._status.text() == "A workflow is already in progress."
    assert shell._active_workflow_id == wfid1, "the app still tracks the first workflow"


# -- 13-17. confirm/execute split + immediate card -------------------------

def test_plan_click_confirms_step1(shell) -> None:
    _show_coding_recommendation(shell)
    calls: list[str] = []
    original = shell.workflow_coordinator.confirm_step
    shell.workflow_coordinator.confirm_step = lambda plan, step_id: (
        calls.append(step_id) or original(plan, step_id)
    )
    try:
        _click_plan_with_claude(shell)
    finally:
        shell.workflow_coordinator.confirm_step = original
    assert calls == ["step_1"], "Plan with Claude must confirm exactly Step 1"


def test_confirm_and_execute_separate(shell) -> None:
    _show_coding_recommendation(shell)
    order: list[str] = []
    confirm_orig = shell.workflow_coordinator.confirm_step
    exec_orig = shell.plan_executor.execute
    shell.workflow_coordinator.confirm_step = lambda plan, step_id: (
        order.append("confirm") or confirm_orig(plan, step_id)
    )
    shell.plan_executor.execute = lambda plan, step_id: (
        order.append("execute") or exec_orig(plan, step_id)
    )
    try:
        _click_plan_with_claude(shell)
    finally:
        shell.workflow_coordinator.confirm_step = confirm_orig
        shell.plan_executor.execute = exec_orig
    assert order == ["confirm", "execute"], "confirm and execute must be two separate calls"


def test_workflow_card_shown_immediately(shell) -> None:
    plan = _start_workflow(shell)
    assert shell.workflow_card.isVisible()
    assert shell.workflow_card.workflow_id == plan.workflow_id
    assert shell.workflow_card.has_active_workflow


def test_initial_step1_status(shell) -> None:
    _show_coding_recommendation(shell)
    with patch.object(shell.plan_executor, "execute"):
        shell._on_recommendation_plan_with_claude(
            shell.recommendation_card.pending_prompt,
            shell.recommendation_card.pending_workspace,
        )
    # Step 1 was confirmed (READY) but not yet executed -> "Preparing…"
    plan = shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)
    assert plan.steps[0].state == WorkflowStepState.READY
    assert shell.workflow_card._rows[0]._status.text() == "Preparing…"


def test_step_started_planning(shell) -> None:
    _show_coding_recommendation(shell)
    with patch.object(shell.plan_executor, "execute"):
        shell._on_recommendation_plan_with_claude(
            shell.recommendation_card.pending_prompt,
            shell.recommendation_card.pending_workspace,
        )
    assert shell.workflow_card._rows[0]._status.text() == "Preparing…"
    plan = shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)
    shell.workflow_coordinator.mark_step_started(plan, "step_1")
    assert shell.workflow_card._rows[0]._status.text() == "Planning…"
    assert shell.workflow_card._rows[0]._dot.state == "working"


# -- 18-20. UI consumes only Workflow semantics -----------------------------

def test_ui_does_not_consume_provider_events(shell=None) -> None:
    hits = _forbidden_in_module(
        WORKFLOW_CARD_FILE,
        (
            "agent_events",
            "agent_adapters",
            "content_block_delta",
            "stream_event",
            "thread.started",
            "item.completed",
            "stream-json",
            "json",
            "QuickAskRunner",
            "process_launcher",
        ),
    )
    assert not hits, f"the card must not touch provider/runner internals: {hits}"


def test_ui_does_not_parse_agent_events(shell=None) -> None:
    hits = _forbidden_in_module(
        WORKFLOW_CARD_FILE, ("AgentEvent", "agent_event", "feed_line", "make_adapter")
    )
    assert not hits, f"the card must not parse AgentEvents: {hits}"
    # The card receives only WorkflowEvent-driven refreshes from the coordinator.
    assert "on_workflow_event" in WORKFLOW_CARD_FILE.read_text(encoding="utf-8")


def test_artifact_attached_does_not_show_internal_id(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    ref = final.steps[0].attached_artifacts[0]
    assert ref.kind == ArtifactKind.PLAN
    text = _card_text(shell.workflow_card)
    assert plan.workflow_id not in text
    assert ref.artifact_id not in text
    assert ref.path not in text
    assert "step_1" not in text


# -- 21-27. plan success target state ---------------------------------------

def test_step_succeeded_plan_ready(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    assert shell.workflow_card._rows[0]._status.text() == "Plan ready"
    assert shell.workflow_coordinator.get_plan(plan.workflow_id).steps[0].state == WorkflowStepState.SUCCEEDED


def test_step2_waiting_confirmation(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    assert shell.workflow_card._rows[1]._status.text() == "Waiting for confirmation"
    assert shell.workflow_coordinator.get_plan(plan.workflow_id).steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION


def test_step3_pending(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    assert shell.workflow_card._rows[2]._status.text() == "Pending"
    assert shell.workflow_coordinator.get_plan(plan.workflow_id).steps[2].state == WorkflowStepState.PENDING


def test_step2_not_auto_confirmed(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    assert final.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert final.current_step_index == 1


def test_step2_not_marked_started(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    assert final.steps[1].state != WorkflowStepState.RUNNING
    assert final.steps[1].state != WorkflowStepState.READY


def test_codex_process_not_started(shell) -> None:
    _show_coding_recommendation(shell)
    ask_mock = _click_plan_with_claude(shell)
    _drive_plan_success(shell)
    calls = ask_mock.call_args_list
    assert len(calls) == 1, "exactly one managed step may run"
    assert set(calls[0].kwargs) == {"prompt"}, "the only managed step is Claude Plan (direct provider)"
    hits = _forbidden_in_module(
        WORKFLOW_CARD_FILE, ("build_codex_args", "CodexJsonlAdapter", "startDetached", "taskkill", "launch_agent")
    )
    assert not hits, f"the card must never start Codex: {hits}"


def test_codex_online_zero(shell=None) -> None:
    hits = _forbidden_in_module(
        WORKFLOW_CARD_FILE,
        ("build_codex_args", "CodexJsonlAdapter", "codex exec", "find_executable", "startDetached"),
    )
    assert not hits, f"the card has no online/process path: {hits}"


# -- 28-30. Open plan --------------------------------------------------------

def test_open_plan_uses_validated_path(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    ref = final.steps[0].attached_artifacts[0]
    validated = shell.artifact_store.resolve_path(ref)
    assert shell.artifact_store.exists(ref)
    opened: list[str] = []
    with patch("app.QDesktopServices.openUrl") as open_mock:
        open_mock.side_effect = lambda url: opened.append(url.toLocalFile()) or True
        shell.workflow_card._on_open()  # Open plan
    assert open_mock.call_count == 1
    assert opened and Path(opened[0]).resolve() == validated.resolve(), (
        "the opened path must be the validated one"
    )
    assert Path(opened[0]).resolve().is_relative_to(shell.artifact_store.root.resolve())


def test_ui_rejects_arbitrary_path(shell) -> None:
    from core.workflow_models import ArtifactRef

    plan = _start_workflow(shell)
    # A malicious ref whose path escapes the artifact root is attached while
    # Step 1 is RUNNING (the coordinator validates kind/producer, not the path).
    evil = ArtifactRef(
        artifact_id="evil1",
        kind=ArtifactKind.PLAN,
        producer_step_id="step_1",
        path="../../escape.md",
        created_at=1,
    )
    shell.workflow_coordinator.attach_artifact(plan, "step_1", evil)
    with patch("app.QDesktopServices.openUrl") as open_mock:
        shell._on_workflow_open_plan(plan.workflow_id)
    assert open_mock.call_count == 0, "an escaping path must never reach the OS opener"
    assert "Plan file unavailable" in shell.workflow_card._status.text()


def test_open_failure_no_crash(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    with patch.object(shell.artifact_store, "resolve_path", side_effect=OSError("boom")):
        shell._on_workflow_open_plan(plan.workflow_id)  # must not raise
    assert "Plan file unavailable" in shell.workflow_card._status.text()
    with patch("app.QDesktopServices.openUrl", return_value=False):
        shell._on_workflow_open_plan(plan.workflow_id)  # OS refused -> no crash
    assert "Couldn't open the plan file" in shell.workflow_card._status.text()


# -- 31-35. hide-not-cancel + recovery ---------------------------------------

def test_close_card_does_not_cancel(shell) -> None:
    plan = _start_workflow(shell)
    shell.workflow_card.dismiss()
    assert not shell.workflow_card.isVisible()
    assert shell.workflow_coordinator.get_plan(plan.workflow_id).state == WorkflowState.RUNNING


def test_business_popover_hide_not_cancel(shell) -> None:
    shell.coordinator.show_shell()
    plan = _start_workflow(shell)
    assert shell.workflow_card.isVisible()
    shell.coordinator._show_context("sessions")
    assert not shell.workflow_card.isVisible()
    assert shell.workflow_coordinator.get_plan(plan.workflow_id).state == WorkflowState.RUNNING


def test_permission_card_hide_not_cancel(shell) -> None:
    shell.coordinator.show_shell()
    plan = _start_workflow(shell)
    assert shell.workflow_card.isVisible()
    shell.coordinator.on_agent_state(
        "claude", AgentState("claude", LifecycleState.WAITING, 1000, "hook")
    )
    assert shell.coordinator.permission_card.isVisible()
    assert not shell.workflow_card.isVisible()
    assert shell.workflow_coordinator.get_plan(plan.workflow_id).state == WorkflowState.RUNNING
    # Permission clearing must NOT auto-repop the workflow card.
    shell.coordinator.on_agent_state(
        "claude", AgentState("claude", LifecycleState.IDLE, 2000, "hook")
    )
    assert not shell.workflow_card.isVisible()


def test_hidden_workflow_keeps_receiving_state(shell) -> None:
    plan = _start_workflow(shell)
    shell.workflow_card.dismiss()
    assert not shell.workflow_card.isVisible()
    _drive_plan_success(shell)  # hidden: still receives WorkflowEvents
    shell.workflow_card.resume_show()
    assert shell.workflow_card._rows[0]._status.text() == "Plan ready"
    assert shell.workflow_card._rows[1]._status.text() == "Waiting for confirmation"


def test_active_workflow_recoverable_via_pet_click(shell) -> None:
    plan = _start_workflow(shell)
    shell.workflow_card.dismiss()
    assert not shell.workflow_card.isVisible()
    shell.coordinator.toggle_bubble()  # user clicks Firefly
    assert shell.workflow_card.isVisible()
    assert shell.workflow_card.workflow_id == plan.workflow_id


# -- 36-39. failure / cancel UI ----------------------------------------------

def test_plan_error_failed_ui(shell) -> None:
    plan = _start_workflow(shell)
    ex = shell.plan_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
    ex._on_finished("", 1)
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    assert final.state == WorkflowState.FAILED
    assert final.steps[0].state == WorkflowStepState.FAILED
    assert shell.workflow_card._rows[0]._status.text() == "Failed"
    assert shell.workflow_card._status.text() == "Failed"


def test_plan_cancelled_ui(shell) -> None:
    plan = _start_workflow(shell)
    ex = shell.plan_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
    ex._on_finished("", 1)
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    assert final.state == WorkflowState.CANCELLED
    assert final.steps[0].state == WorkflowStepState.CANCELLED
    assert final.steps[1].state == WorkflowStepState.SKIPPED
    assert shell.workflow_card._rows[0]._status.text() == "Cancelled"
    assert shell.workflow_card._status.text() == "Cancelled"


def test_explicit_cancel_running_uses_executor(shell) -> None:
    plan = _start_workflow(shell)
    with patch.object(shell.plan_executor, "stop") as stop_mock:
        shell.workflow_card._on_cancel()
    assert stop_mock.call_count == 1, "a running Plan must cancel through the executor"
    assert shell.workflow_coordinator.get_plan(plan.workflow_id).state == WorkflowState.RUNNING
    # The real transport CANCELLED event completes the coordinator cancel.
    ex = shell.plan_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
    ex._on_finished("", 1)
    assert shell.workflow_coordinator.get_plan(plan.workflow_id).state == WorkflowState.CANCELLED


def test_explicit_cancel_waiting_uses_coordinator(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    assert not shell.plan_executor.running
    shell.workflow_card._on_cancel()
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    assert final.state == WorkflowState.CANCELLED
    assert final.steps[1].state == WorkflowStepState.CANCELLED
    assert final.steps[2].state == WorkflowStepState.SKIPPED


# -- 40-41. no retry / no fallback -------------------------------------------

def test_no_automatic_retry(shell) -> None:
    _show_coding_recommendation(shell)
    ask_mock = _click_plan_with_claude(shell)
    ex = shell.plan_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
    ex._on_failed("boom")
    assert shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id).state == WorkflowState.FAILED
    assert ask_mock.call_count == 1, "a failed step must never be re-run"


def test_no_fallback_agent(shell) -> None:
    _show_coding_recommendation(shell)
    ask_mock = _click_plan_with_claude(shell)
    ex = shell.plan_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
    ex._on_failed("boom")
    assert ask_mock.call_count == 1, "no fallback agent may be used"


# -- 42-45. isolation + artifact ---------------------------------------------

def test_session_manager_unchanged(shell) -> None:
    ws = shell.workspace_manager.current()
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    assert shell.session_manager.get_native_id("claude", ws) is None
    assert shell.session_manager.get_native_id("codex", ws) is None


def test_sessions_json_unchanged(shell) -> None:
    sessions_file = PROJECT_DIR / "config" / "sessions.json"
    before = sessions_file.read_bytes() if sessions_file.exists() else None
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    if before is None:
        assert not sessions_file.exists(), "config/sessions.json must not be created"
    else:
        assert sessions_file.read_bytes() == before


def test_claude_source_lifecycle_unchanged(shell) -> None:
    sources = PROJECT_DIR / "runtime" / "sources"
    before: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            before[p.name] = p.read_bytes()
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    after: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            after[p.name] = p.read_bytes()
    assert before == after, "a workflow Plan must never touch external lifecycle sources"


def test_plan_md_exists_in_full(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    ref = final.steps[0].attached_artifacts[0]
    assert shell.artifact_store.read_text(ref) == PLAN_TEXT
    raw = (shell.artifact_store.root / plan.workflow_id / "plan.md").read_text(encoding="utf-8")
    assert raw == PLAN_TEXT


# -- 46-48. no internal content / no full plan / Light Glass ------------------

def test_ui_does_not_show_full_plan(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    text = _card_text(shell.workflow_card)
    assert PLAN_TEXT not in text
    assert "refactor the auth module" not in text
    assert "Open plan" in shell.workflow_card._open_btn.text()


def test_ui_does_not_show_internal_tokens(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    text = _card_text(shell.workflow_card)
    for token in ("workflow_created", "STEP_SUCCEEDED", "managed_agent_result", "T0", "T6"):
        assert token not in text, f"internal token leaked: {token}"
    assert plan.workflow_id not in text
    assert plan.steps[0].step_id not in text


def test_light_glass_style(shell) -> None:
    plan = _start_workflow(shell)
    card = shell.workflow_card
    assert isinstance(card, PopoverBase)
    assert isinstance(card._card, theme.GlassPanel)
    assert 330 <= card.width() <= 370, "target width ~330-370px"
    assert card._open_btn.styleSheet() == theme.popover_button_style("workflowOpen")
    # Very light status dots on each row.
    for row in card._rows:
        assert row._dot is not None
        assert row._dot.width() <= 12


# -- 49-52. regression --------------------------------------------------------

def test_recommendation_card_regression(shell) -> None:
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")) as launch_mock:
        _show_coding_recommendation(shell)
        shell.recommendation_card._primary_btn.click()
    assert launch_mock.call_count == 1
    assert shell.recommendation_card.handoff_state.value == "handed_off"


def test_short_talk_regression(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True):
        _open_input(shell)
        shell._on_short_ask_send("解释这段代码")
    assert shell.short_ask.running
    assert shell.workflow_card.workflow_id is None, "a chat must not create a workflow"
    shell.short_ask.on_agent_event(
        AgentEvent.make("claude", AgentEventType.FINAL, text="done")
    )
    assert shell.short_ask.full_answer() == "done"


def test_handoff_regression(shell) -> None:
    locked = shell.workspace_manager.current()
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")) as launch_mock:
        _show_coding_recommendation(shell)
        shell.recommendation_card._primary_btn.click()
    assert launch_mock.call_args[0][1] == str(locked), "handoff keeps the locked workspace"
    assert shell.workflow_card.workflow_id is None


def test_coordinator_qt_free(shell=None) -> None:
    hits = _forbidden_in_module(
        COORDINATOR_FILE,
        (
            "workflow_executor",
            "process_launcher",
            "QuickAskRunner",
            "artifact_store",
            "PySide6",
            "QProcess",
            "QObject",
            "workflow_card",
        ),
    )
    assert not hits, f"coordinator must stay Qt-free / UI-free: {hits}"


# -- 53-56. no persistence / no step 2 / no review ----------------------------

def test_no_workflow_persistence(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    assert not (PROJECT_DIR / "config" / "workflows.json").exists()
    assert not (PROJECT_DIR / "runtime" / "workflows").exists()


def test_no_changed_files_implementation(shell) -> None:
    plan = _start_workflow(shell)
    _drive_plan_success(shell)
    final = shell.workflow_coordinator.get_plan(plan.workflow_id)
    assert final.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert final.steps[1].attached_artifacts == ()
    assert not (shell.artifact_store.root / plan.workflow_id / "changed_files.md").exists()
    assert not (shell.artifact_store.root / plan.workflow_id / "implementation_summary.md").exists()


def test_no_review_execution(shell) -> None:
    _show_coding_recommendation(shell)
    ask_mock = _click_plan_with_claude(shell)
    _drive_plan_success(shell)
    final = shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)
    assert final.steps[2].state == WorkflowStepState.PENDING
    assert final.steps[2].attached_artifacts == ()
    assert ask_mock.call_count == 1, "only Claude Plan may run"


def test_no_automatic_step2_execution(shell) -> None:
    _show_coding_recommendation(shell)
    ask_mock = _click_plan_with_claude(shell)
    _drive_plan_success(shell)
    assert ask_mock.call_count == 1
    final = shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)
    assert final.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert final.steps[1].state != WorkflowStepState.RUNNING


# -- desktop smoke -------------------------------------------------------------

def _desktop_smoke(shell) -> None:
    _prepare(shell)
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("把这个模块重构一下")
    card = shell.recommendation_card
    assert card._status.text() == "Recommended · Codex"
    assert card._primary_btn.text() == "Send to Codex"
    assert card._secondary_btn.text() == "Plan with Claude"
    assert card._light_btn.text() == "Open only"
    with patch.object(shell.plan_executor._runner, "ask", return_value=True) as ask_mock:
        card._secondary_btn.click()
    assert shell.workflow_card.isVisible()
    assert shell.workflow_card._rows[0]._status.text() == "Planning…"
    _drive_plan_success(shell)
    final = shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)
    assert final.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert final.state == WorkflowState.WAITING_FOR_USER
    assert shell.workflow_card._rows[0]._status.text() == "Plan ready"
    assert shell.workflow_card._rows[1]._status.text() == "Waiting for confirmation"
    assert shell.workflow_card._rows[2]._status.text() == "Pending"
    assert ask_mock.call_count == 1
    assert set(ask_mock.call_args_list[0].kwargs) == {"prompt"}, "Codex must never start"
    print(
        "Smoke: '把这个模块重构一下' -> Plan with Claude -> "
        "Claude·Plan Plan ready / Codex·Implement Waiting for confirmation / "
        "Claude·Review Pending  PASS"
    )


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    tmp = tempfile.mkdtemp(prefix="fap9d3_")
    shell = VisualShell(None, artifact_root=Path(tmp))
    tests = [
        test_coding_primary_send_codex,
        test_coding_secondary_plan_with_claude,
        test_coding_light_open_only,
        test_ask_claude_first_not_shown,
        test_send_to_codex_keeps_9c_contract,
        test_send_to_codex_no_workflow,
        test_open_only_no_workflow,
        test_plan_with_claude_creates_workflow,
        test_original_task_request_preserved,
        test_workspace_locked,
        test_workspace_drift_blocks_workflow,
        test_second_workflow_rejected,
        test_plan_click_confirms_step1,
        test_confirm_and_execute_separate,
        test_workflow_card_shown_immediately,
        test_initial_step1_status,
        test_step_started_planning,
        test_ui_does_not_consume_provider_events,
        test_ui_does_not_parse_agent_events,
        test_artifact_attached_does_not_show_internal_id,
        test_step_succeeded_plan_ready,
        test_step2_waiting_confirmation,
        test_step3_pending,
        test_step2_not_auto_confirmed,
        test_step2_not_marked_started,
        test_codex_process_not_started,
        test_codex_online_zero,
        test_open_plan_uses_validated_path,
        test_ui_rejects_arbitrary_path,
        test_open_failure_no_crash,
        test_close_card_does_not_cancel,
        test_business_popover_hide_not_cancel,
        test_permission_card_hide_not_cancel,
        test_hidden_workflow_keeps_receiving_state,
        test_active_workflow_recoverable_via_pet_click,
        test_plan_error_failed_ui,
        test_plan_cancelled_ui,
        test_explicit_cancel_running_uses_executor,
        test_explicit_cancel_waiting_uses_coordinator,
        test_no_automatic_retry,
        test_no_fallback_agent,
        test_session_manager_unchanged,
        test_sessions_json_unchanged,
        test_claude_source_lifecycle_unchanged,
        test_plan_md_exists_in_full,
        test_ui_does_not_show_full_plan,
        test_ui_does_not_show_internal_tokens,
        test_light_glass_style,
        test_recommendation_card_regression,
        test_short_talk_regression,
        test_handoff_regression,
        test_coordinator_qt_free,
        test_no_workflow_persistence,
        test_no_changed_files_implementation,
        test_no_review_execution,
        test_no_automatic_step2_execution,
    ]
    try:
        for fn in tests:
            _prepare(shell)
            fn(shell)
        print(f"Phase 9D.3 workflow UX tests passed ({len(tests)} tests).")
        _desktop_smoke(shell)
    finally:
        shell.shutdown()


if __name__ == "__main__":
    main()
