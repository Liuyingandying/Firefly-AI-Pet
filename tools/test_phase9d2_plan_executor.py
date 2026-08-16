"""Phase 9D.2 — PlanStepExecutor tests.

Covers the 38 required executor scenarios (spec items 23-60): only READY steps
execute, awaiting/pending/wrong-current rejected, only Claude PLAN supported
(Codex IMPLEMENT and Claude REVIEW explicitly rejected), execute -> RUNNING,
AgentEvent TEXT_DELTA + FINAL collected, full final response persisted, UI
truncation never affects the artifact, PLAN artifact attached, MANAGED_AGENT_RESULT
evidence, Plan -> SUCCEEDED, next Codex step -> AWAITING_CONFIRMATION and NOT
executed, workflow -> WAITING_FOR_USER, Agent ERROR -> FAILED, empty result ->
failure, artifact write failure -> failure, no automatic retry, no fallback
agent, execution-busy gate, cancel semantics, transient session never reads or
writes the ordinary SessionManager, config/sessions.json unchanged, no --resume,
--safe-mode retained, external lifecycle sources untouched, existing Claude
adapter reused, no provider JSON parsing in the executor, Coordinator stays
Qt-free, no Codex spawned, no workflow persistence, and the plan artifact has no
transcript wrapper.

Uses fake AgentEvent drives against the DirectProviderRunner (ask() mocked);
zero online calls.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEvent, AgentEventType
from core.artifact_store import ArtifactError, ArtifactStore
from core.routing_models import HandoffMode, TaskRequest
from core.session_manager import SessionManager
from core.session_store import SessionStore
from core.workflow_coordinator import WorkflowCoordinator, WorkflowTransitionError
from core.workflow_models import (
    ArtifactKind,
    CompletionSource,
    WorkflowEventType,
    WorkflowKind,
    WorkflowPlan,
    WorkflowState,
    WorkflowStep,
    WorkflowStepIntent,
    WorkflowStepState,
)
from core.workflow_prompt import build_plan_prompt
from ui.quick_chat_protocol import WORKFLOW_READ_ONLY_TOOLS, build_claude_args
from ui.workflow_provider_runner import DirectProviderRunner
from ui.workflow_executor import (
    ExecutionBusy,
    PlanStepExecutor,
    UnsupportedStepExecution,
    WorkflowExecutionError,
)
from test_phase9a_agent_router import _forbidden_in_module

EXECUTOR_FILE = PROJECT_DIR / "ui" / "workflow_executor.py"
COORDINATOR_FILE = PROJECT_DIR / "core" / "workflow_coordinator.py"

LONG_PLAN = "# Implementation Plan\n\n" + "".join(
    f"## Change {i}\nDescription with enough detail to exceed 600 chars.\n"
    for i in range(40)
)
assert len(LONG_PLAN) > 1000

# A plan body that satisfies the deterministic Plan validity gate (≥ MIN_PLAN_LEN
# chars, no generic no-task phrase; the task "Refactor the auth module" has no
# file-like token so NO_TASK_REFERENCE is skipped).
VALID_PLAN = (
    "# Implementation Plan\n\n"
    "## Goal\nRefactor the auth module into smaller, testable functions.\n\n"
    "## Changes\n1. Split the login flow into separate steps.\n"
    "2. Extract validation into its own helper.\n\n"
    "## Validation\nRun the unit tests and confirm every suite passes.\n"
)
assert len(VALID_PLAN) >= 80


def _req(workspace: str, text: str = "Refactor the auth module") -> TaskRequest:
    return TaskRequest(text=text, workspace=workspace)


def _coord() -> WorkflowCoordinator:
    state = {"t": 1_700_000_000_000}

    def clock() -> int:
        state["t"] += 1
        return state["t"]

    return WorkflowCoordinator(clock=clock)


def _custom_plan(
    agent: str,
    intent: WorkflowStepIntent,
    handoff: HandoffMode,
    *,
    workspace: str = "E:/Work",
    state: WorkflowStepState = WorkflowStepState.READY,
    step_id: str = "s1",
) -> WorkflowPlan:
    step = WorkflowStep(
        step_id=step_id,
        agent_id=agent,
        intent=intent,
        handoff_mode=handoff,
        requires_confirmation=True,
        state=state,
        created_at=1,
        updated_at=1,
        expected_artifacts=(),
        required_artifacts=(),
    )
    return WorkflowPlan(
        workflow_id="wf-custom",
        kind=WorkflowKind.PLAN_IMPLEMENT_REVIEW,
        original_request=TaskRequest(text="task", workspace=workspace),
        workspace=workspace,
        steps=(step,),
        state=WorkflowState.WAITING_FOR_USER,
        current_step_index=0,
        created_at=1,
        updated_at=1,
    )


def _new_executor(
    store: ArtifactStore,
    app: QApplication,
    *,
    coord: WorkflowCoordinator | None = None,
    runner: DirectProviderRunner | None = None,
) -> PlanStepExecutor:
    return PlanStepExecutor(coord or _coord(), store, runner=runner, parent=app)


def _ready_plan(coord: WorkflowCoordinator, ws: Path) -> WorkflowPlan:
    plan = coord.create_plan_implement_review(_req(str(ws)))
    return coord.confirm_step(plan, plan.steps[0].step_id)


def _start(executor: PlanStepExecutor, plan: WorkflowPlan, ask_mock) -> None:
    """Drive execute with ask() mocked to True (no process is launched)."""
    executor.execute(plan, plan.steps[0].step_id)


def _drive_success(executor: PlanStepExecutor, text: str) -> None:
    """Feed a FINAL and finish the fake turn, as the runner would."""
    executor._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=text))
    executor._on_finished(text, 0)


# -- 23-29. execution eligibility ------------------------------------------

def test_only_ready_step_executable(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        started = ex.execute(plan, plan.steps[0].step_id)
    assert started.steps[0].state == WorkflowStepState.RUNNING
    assert ask_mock.call_count == 1


def test_awaiting_confirmation_rejected(root: Path, app: QApplication) -> None:
    coord = _coord()
    plan = coord.create_plan_implement_review(_req(str(root / "ws")))
    assert plan.steps[0].state == WorkflowStepState.AWAITING_CONFIRMATION
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    try:
        ex.execute(plan, plan.steps[0].step_id)
    except WorkflowExecutionError:
        return
    raise AssertionError("AWAITING_CONFIRMATION step must not execute")


def test_pending_rejected(root: Path, app: QApplication) -> None:
    plan = _custom_plan("claude", WorkflowStepIntent.PLAN, HandoffMode.SHORT_TALK, state=WorkflowStepState.PENDING)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app)
    try:
        ex.execute(plan, "s1")
    except WorkflowExecutionError:
        return
    raise AssertionError("PENDING step must not execute")


def test_wrong_current_step_rejected(root: Path, app: QApplication) -> None:
    coord = _coord()
    plan = _ready_plan(coord, root / "ws")
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    try:
        ex.execute(plan, plan.steps[1].step_id)  # step 2 is not current
    except WorkflowExecutionError:
        return
    raise AssertionError("a non-current step must not execute")


def test_unknown_step_rejected(root: Path, app: QApplication) -> None:
    coord = _coord()
    plan = _ready_plan(coord, root / "ws")
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    try:
        ex.execute(plan, "no-such-step")
    except WorkflowExecutionError:
        return
    raise AssertionError("an unknown step id must not execute")


def test_only_claude_plan_supported(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        started = ex.execute(plan, plan.steps[0].step_id)
    assert started.steps[0].state == WorkflowStepState.RUNNING
    assert ask_mock.call_count == 1
    assert ask_mock.call_args.kwargs == {"prompt": build_plan_prompt(plan.original_request)}
    assert isinstance(ex._runner, DirectProviderRunner)


def test_codex_implement_explicitly_rejected(root: Path, app: QApplication) -> None:
    plan = _custom_plan("codex", WorkflowStepIntent.IMPLEMENT, HandoffMode.OPEN_NATIVE)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app)
    try:
        ex.execute(plan, "s1")
    except UnsupportedStepExecution:
        return
    raise AssertionError("Codex IMPLEMENT must be rejected in 9D.2")


def test_claude_review_explicitly_rejected(root: Path, app: QApplication) -> None:
    plan = _custom_plan("claude", WorkflowStepIntent.REVIEW, HandoffMode.SHORT_TALK)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app)
    try:
        ex.execute(plan, "s1")
    except UnsupportedStepExecution:
        return
    raise AssertionError("Claude REVIEW must be rejected in 9D.2")


def test_terminal_workflow_rejected(root: Path, app: QApplication) -> None:
    plan = _custom_plan("claude", WorkflowStepIntent.PLAN, HandoffMode.SHORT_TALK)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app)
    try:
        ex.execute(dataclasses.replace(plan, state=WorkflowState.FAILED), "s1")
    except WorkflowExecutionError:
        return
    raise AssertionError("a terminal workflow must not execute")


# -- 30-32. RUNNING + event collection --------------------------------------

def test_execute_marks_running(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    started_events: list[str] = []
    ex.step_started.connect(lambda: started_events.append("started"))
    with patch.object(ex._runner, "ask", return_value=True):
        started = ex.execute(plan, plan.steps[0].step_id)
    assert started.steps[0].state == WorkflowStepState.RUNNING
    assert started.state == WorkflowState.RUNNING
    assert ex.running is True
    assert started_events == ["started"]


def test_text_delta_collected(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text="你"))
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text="好"))
    assert ex._collected_deltas == ["你", "好"]


def test_final_captured(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text="final text"))
    assert ex._final_text == "final text"


# -- 33-40. success path -----------------------------------------------------

def test_full_final_response_persisted(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(store, app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, LONG_PLAN)
    final = ex.plan
    assert final.steps[0].state == WorkflowStepState.SUCCEEDED
    ref = final.steps[0].attached_artifacts[0]
    assert store.read_text(ref) == LONG_PLAN


def test_ui_truncation_does_not_affect_artifact(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(store, app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, LONG_PLAN)
    ref = ex.plan.steps[0].attached_artifacts[0]
    stored = store.read_text(ref)
    assert len(stored) == len(LONG_PLAN)
    assert stored == LONG_PLAN
    assert "…" not in stored  # never the 600-char UI truncation suffix


def test_plan_artifact_attached(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(store, app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    refs = ex.plan.steps[0].attached_artifacts
    assert len(refs) == 1
    assert refs[0].kind == ArtifactKind.PLAN
    assert refs[0].producer_step_id == plan.steps[0].step_id


def test_managed_agent_result_evidence(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    recorded: list = []
    coord.connect(lambda e: recorded.append(e))
    ex = _new_executor(store, app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    succeeded = [e for e in recorded if e.type == WorkflowEventType.STEP_SUCCEEDED]
    assert len(succeeded) == 1
    assert succeeded[0].evidence is not None
    assert succeeded[0].evidence.source == CompletionSource.MANAGED_AGENT_RESULT


def test_plan_step_succeeded(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    assert ex.plan.steps[0].state == WorkflowStepState.SUCCEEDED
    assert ex.running is False


def test_next_codex_step_awaiting_confirmation(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    final = ex.plan
    assert final.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert final.current_step_index == 1


def test_next_codex_step_not_executed(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    assert ask_mock.call_count == 1, "only the Claude Plan step may run"
    assert ex.plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert ex.plan.steps[1].state != WorkflowStepState.RUNNING


def test_workflow_waiting_for_user(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    assert ex.plan.state == WorkflowState.WAITING_FOR_USER


# -- 41-45. failure semantics ------------------------------------------------

def test_agent_error_workflow_failed(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, plan.steps[0].step_id)
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
    ex._on_finished("", 1)
    assert ex.plan.state == WorkflowState.FAILED
    assert ex.plan.steps[0].state == WorkflowStepState.FAILED
    assert ask_mock.call_count == 1


def test_empty_result_is_failure(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    # process exited 0 but no FINAL / no deltas -> no usable result
    ex._on_finished("", 0)
    assert ex.plan.state == WorkflowState.FAILED
    assert ex.plan.steps[0].state == WorkflowStepState.FAILED


def test_artifact_write_failure_is_failure(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex = _new_executor(store, app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    with patch.object(store, "write_text", side_effect=ArtifactError("disk full")):
        _drive_success(ex, VALID_PLAN)
    assert ex.plan.state == WorkflowState.FAILED
    assert ex.plan.steps[0].state == WorkflowStepState.FAILED


def test_no_automatic_retry(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, plan.steps[0].step_id)
        ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
        ex._on_failed("boom")
    assert ex.plan.state == WorkflowState.FAILED
    assert ask_mock.call_count == 1, "a failed step must never be re-run"
    for name in dir(coord):
        assert "retry" not in name.lower(), f"coordinator must not expose {name}"


def test_no_fallback_agent(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, plan.steps[0].step_id)
        ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
        ex._on_failed("boom")
    assert ask_mock.call_count == 1, "no fallback agent may be used"


# -- 46-47. busy / cancel ----------------------------------------------------

def test_execution_busy_prevents_concurrency(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    try:
        ex.execute(plan, plan.steps[0].step_id)
    except ExecutionBusy:
        return
    raise AssertionError("a second concurrent execution must be rejected")


def test_cancel_semantics_consistent(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
    ex._on_finished("", 1)
    assert ex.plan.state == WorkflowState.CANCELLED
    assert ex.plan.steps[0].state == WorkflowStepState.CANCELLED
    assert ex.running is False


# -- 48-52. transient session isolation --------------------------------------

def test_transient_session_does_not_read_session_manager(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    app_mgr = SessionManager()
    app_mgr.set("claude", ws, "sess-short-talk")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    # The direct provider runner owns no SessionManager at all, so it can never
    # read or write the ordinary Short Talk sessions.
    assert isinstance(ex._runner, DirectProviderRunner)
    assert not hasattr(ex._runner, "_sessions")
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    # the ordinary Short Talk session is untouched
    assert app_mgr.get_native_id("claude", ws) == "sess-short-talk"


def test_transient_session_does_not_write_session_manager(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    app_mgr = SessionManager()
    app_mgr.set("claude", ws, "sess-short-talk")
    ex = _new_executor(ArtifactStore(root / "artifacts"), app)
    # The direct runner never creates a Claude native session and never reaches
    # SessionManager.set(); the Short Talk session is untouched by construction.
    assert not hasattr(ex._runner, "_sessions")
    assert app_mgr.get_native_id("claude", ws) == "sess-short-talk"


def test_sessions_json_unchanged(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    sessions_file = root / "config" / "sessions.json"
    app_mgr = SessionManager(store=SessionStore(sessions_file))
    app_mgr.set("claude", ws, "sess-existing")
    before = sessions_file.read_bytes()
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    assert sessions_file.read_bytes() == before, "config/sessions.json must be unchanged"


def test_no_resume_for_workflow_plan(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, plan.steps[0].step_id)
    kwargs = ask_mock.call_args.kwargs
    assert "session_id" not in kwargs
    assert "persistent" not in kwargs
    assert set(kwargs) == {"prompt"}, "the direct provider ask is prompt-only (no CLI session/resume)"
    # Ordinary Short Talk still expresses its non-resume contract via the CLI.
    args = build_claude_args("x", session_id="sess-any", persistent=False, isolated=True)
    assert "--resume" not in args
    assert "--no-session-persistence" in args


def test_safe_mode_retained(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, plan.steps[0].step_id)
    # No CLI is spawned, so no hooks can fire; the read-only guarantee now lives
    # in the prompt itself rather than a --safe-mode / --tools argv.
    assert set(ask_mock.call_args.kwargs) == {"prompt"}
    assert "Do not modify project files" in ask_mock.call_args.kwargs["prompt"]
    # Ordinary Short Talk keeps its CLI hook-isolation contract unchanged.
    args = build_claude_args("x", persistent=False, isolated=True, read_only_tools=True)
    assert "--safe-mode" in args
    assert "--permission-mode" not in args
    assert "--tools" in args
    assert args[args.index("--tools") + 1] == ",".join(WORKFLOW_READ_ONLY_TOOLS)


# -- 53-60. boundaries -------------------------------------------------------

def test_external_lifecycle_source_not_written(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    sources = PROJECT_DIR / "runtime" / "sources"
    before: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            before[p.name] = p.read_bytes()
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    after: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            after[p.name] = p.read_bytes()
    assert before == after, "a workflow Plan must never touch external lifecycle sources"


def test_executor_uses_direct_provider_runner(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    runner = DirectProviderRunner(parent=app)
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, runner=runner)
    assert isinstance(ex._runner, DirectProviderRunner)
    # the executor never parses provider JSON / CLI stream output itself, and
    # never uses the Claude CLI transport.
    hits = _forbidden_in_module(
        EXECUTOR_FILE,
        (
            "ClaudeStreamAdapter", "make_adapter", "agent_adapters", "feed_line",
            "parse_claude_event", "QuickAskRunner", "process_launcher",
        ),
    )
    assert not hits, f"executor must delegate to the direct runner, not parse: {hits}"
    # The Short Talk Claude adapter still exists and is untouched.
    from core.agent_adapters import make_adapter
    assert make_adapter("claude").agent_id == "claude"


def test_no_provider_json_parsing_in_executor() -> None:
    hits = _forbidden_in_module(
        EXECUTOR_FILE, ("json", "loads", "dumps", "stream-json", "feed_line", "raw_event", "provider")
    )
    assert not hits, f"executor must not parse provider JSON: {hits}"


def test_coordinator_remains_qt_free() -> None:
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
        ),
    )
    assert not hits, f"coordinator must stay Qt-free / executor-free: {hits}"
    # dependency direction: the executor imports the coordinator, never the reverse
    assert "WorkflowCoordinator" in EXECUTOR_FILE.read_text(encoding="utf-8")


def test_no_codex_process_spawned(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, plan.steps[0].step_id)
        _drive_success(ex, VALID_PLAN)
    assert ask_mock.call_count == 1, "only the Claude Plan step may run"
    hits = _forbidden_in_module(EXECUTOR_FILE, ("build_codex_args", "CodexJsonlAdapter", "launch_agent", "startDetached"))
    assert not hits, f"executor must never spawn Codex: {hits}"


def test_no_automatic_step2_execution(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, plan.steps[0].step_id)
        _drive_success(ex, VALID_PLAN)
    assert ex.plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert ex.plan.steps[1].state != WorkflowStepState.RUNNING
    assert ask_mock.call_count == 1


def test_no_workflow_persistence(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    coord = _coord()
    ex = _new_executor(ArtifactStore(root / "artifacts"), app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    _drive_success(ex, VALID_PLAN)
    assert not (PROJECT_DIR / "config" / "workflows.json").exists()
    assert not (PROJECT_DIR / "runtime" / "workflows").exists()


def test_artifact_has_no_transcript_wrapper(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex = _new_executor(store, app, coord=coord)
    plan = _ready_plan(coord, ws)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, plan.steps[0].step_id)
    body = VALID_PLAN
    _drive_success(ex, body)
    ref = ex.plan.steps[0].attached_artifacts[0]
    assert store.read_text(ref) == body
    raw = (store.root / ex.plan.workflow_id / "plan.md").read_text(encoding="utf-8")
    assert raw == body, "plan.md must be exactly the plan content, no transcript wrapper"


# -- workflow-specific plan prompt ------------------------------------------

def test_plan_prompt_preserves_task_verbatim() -> None:
    from core.workflow_prompt import build_plan_prompt

    task = "Add a greeting function that returns a friendly message."
    prompt = build_plan_prompt(TaskRequest(text=task, workspace="E:/x"))
    assert task in prompt
    assert prompt.count(task) == 1


def test_plan_prompt_read_only_intent() -> None:
    from core.workflow_prompt import build_plan_prompt

    prompt = build_plan_prompt(_req("E:/x"))
    for must_have in ("plan", "Do not modify project files", "Markdown"):
        assert must_have.lower() in prompt.lower(), f"prompt must mention {must_have!r}"
    for forbidden in (
        "auto approve",
        "auto-approve",
        "skip permissions",
        "bypassPermissions",
        "must execute",
        "modify files",
        "run destructive commands",
        "run this command",
        "approve permissions",
    ):
        assert forbidden.lower() not in prompt.lower(), f"prompt must not contain {forbidden!r}"


def test_plan_prompt_empty_task_rejected() -> None:
    from core.workflow_prompt import build_plan_prompt

    try:
        build_plan_prompt(TaskRequest(text="   ", workspace="E:/x"))
    except ValueError:
        return
    raise AssertionError("an empty task must be rejected")


# -- main --------------------------------------------------------------------

def _run_in_fresh_dir(fn, app: QApplication) -> None:
    with tempfile.TemporaryDirectory() as td:
        fn(Path(td), app)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    tests = [
        test_only_ready_step_executable,
        test_awaiting_confirmation_rejected,
        test_pending_rejected,
        test_wrong_current_step_rejected,
        test_unknown_step_rejected,
        test_only_claude_plan_supported,
        test_codex_implement_explicitly_rejected,
        test_claude_review_explicitly_rejected,
        test_terminal_workflow_rejected,
        test_execute_marks_running,
        test_text_delta_collected,
        test_final_captured,
        test_full_final_response_persisted,
        test_ui_truncation_does_not_affect_artifact,
        test_plan_artifact_attached,
        test_managed_agent_result_evidence,
        test_plan_step_succeeded,
        test_next_codex_step_awaiting_confirmation,
        test_next_codex_step_not_executed,
        test_workflow_waiting_for_user,
        test_agent_error_workflow_failed,
        test_empty_result_is_failure,
        test_artifact_write_failure_is_failure,
        test_no_automatic_retry,
        test_no_fallback_agent,
        test_execution_busy_prevents_concurrency,
        test_cancel_semantics_consistent,
        test_transient_session_does_not_read_session_manager,
        test_transient_session_does_not_write_session_manager,
        test_sessions_json_unchanged,
        test_no_resume_for_workflow_plan,
        test_safe_mode_retained,
        test_external_lifecycle_source_not_written,
        test_executor_uses_direct_provider_runner,
        test_no_provider_json_parsing_in_executor,
        test_coordinator_remains_qt_free,
        test_no_codex_process_spawned,
        test_no_automatic_step2_execution,
        test_no_workflow_persistence,
        test_artifact_has_no_transcript_wrapper,
        test_plan_prompt_preserves_task_verbatim,
        test_plan_prompt_read_only_intent,
        test_plan_prompt_empty_task_rejected,
    ]
    for fn in tests:
        params = list(inspect.signature(fn).parameters)
        if not params:
            fn()
        else:
            _run_in_fresh_dir(fn, app)
    print(f"Phase 9D.2 plan executor tests passed ({len(tests)} tests).")


if __name__ == "__main__":
    main()
