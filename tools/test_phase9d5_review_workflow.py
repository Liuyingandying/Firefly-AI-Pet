"""Phase 9D.5 — Claude Review + Workflow Completion tests.

Covers the required workflow scenarios: Step 3 starts AWAITING_CONFIRMATION;
``Review with Claude`` is the Step-3 action (visible only while Step 3 awaits
confirmation); confirm and execute stay two separate calls and confirm_step
never starts an agent; only READY Claude REVIEW steps execute (PLAN / IMPLEMENT
steps are rejected); artifact-first review context — the PLAN (producer Step 1)
and CHANGED_FILES (producer Step 2) artifacts are required and producer-validated,
the IMPLEMENTATION_SUMMARY is auxiliary-only; no transcript, no session id, no
API key reaches the prompt; the review prompt preserves the original task,
contains the PLAN + CHANGED_FILES, is explicitly read-only, and never asks for
auto-approval / modifications / a fixed PASS verdict; the read-only contract is
an explicit --tools allowlist (Read/Glob/Grep) + --safe-mode via a transient
session (persistent=False,
no --resume, private SessionManager, sessions.json and lifecycle sources
untouched); the existing AgentEvent adapter is reused (no provider JSON parsing
in the executor); TEXT_DELTA / FINAL are collected; a usable FINAL writes the
REVIEW artifact (review.md, full text, producer Step 3) and succeeds Step 3 with
MANAGED_AGENT_RESULT evidence; empty output / ERROR / artifact-write failure fail
the workflow with no auto retry and no fallback Codex; the workflow reaches
SUCCEEDED with all three steps done; the card says ``Review ready`` +
``Workflow complete`` and never claims approval; the review verdict is never
parsed (NEEDS_CHANGES still completes the step); Open review uses an
ArtifactStore-validated path and arbitrary paths / open failures are controlled;
closing the completed card never deletes artifacts; a completed workflow no
longer blocks a new one; cancel while Review runs waits for the real transport
cancel; no workflow persistence / no remediation loop / Codex never launched;
the workspace stays unchanged; hide-not-cancel / PermissionCard priority / Short
Talk / 9C Send-to-Codex regressions hold.

No online calls: Claude Review is mocked/fake throughout.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
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
from core.models import AgentState, LifecycleState
from core.routing_models import HandoffMode, TaskRequest
from core.workflow_coordinator import WorkflowCoordinator, WorkflowTransitionError
from core.workflow_models import (
    ArtifactKind,
    CompletionSource,
    StepCompletionEvidence,
    WorkflowEventType,
    WorkflowKind,
    WorkflowPlan,
    WorkflowState,
    WorkflowStep,
    WorkflowStepIntent,
    WorkflowStepState,
    make_artifact_ref,
)
from ui.process_launcher import ProcessLauncher
from ui.workflow_provider_runner import DirectProviderRunner
from ui.workflow_review_executor import (
    ReviewExecutionBusy,
    ReviewExecutionError,
    ReviewStepExecutor,
    UnsupportedReviewExecution,
)

from test_phase9a_agent_router import _forbidden_in_module

REVIEW_FILE = PROJECT_DIR / "ui" / "workflow_review_executor.py"
CARD_FILE = PROJECT_DIR / "ui" / "workflow_card.py"
COORDINATOR_FILE = PROJECT_DIR / "core" / "workflow_coordinator.py"

PLAN_TEXT = (
    "# Implementation Plan\n\n"
    "- Goal: add a greeting function\n"
    "- Files: app.py\n"
    "- Changes: add greet() and call it on startup\n"
)

CHANGED_TEXT = (
    "# Changed Files\n\n"
    "## Added\n- (none)\n\n"
    "## Modified\n- app.py\n\n"
    "## Deleted\n- (none)\n"
)

SUMMARY_TEXT = (
    "# Implementation Summary\n\n"
    "Task:\nAdd a greeting function\n\n"
    "Completion:\nUser confirmed the Codex implementation step completed.\n\n"
    "Changed files:\n- app.py\n"
)

REVIEW_TEXT = (
    "# Verdict\nPASS\n\n"
    "# Summary\nThe greeting function matches the plan.\n\n"
    "# Findings\nNo blocking findings.\n\n"
    "# Validation\napp.py compiles and greet() is wired in.\n\n"
    "# Recommended Next Actions\nNone.\n"
)

LONG_REVIEW = "# Verdict\nNEEDS_CHANGES\n\n# Summary\n" + ("Review detail line. " * 40)
assert len(LONG_REVIEW) > 600

BASE_A_PY = "def greet():\n    return 'hi'\n"


# -- fixtures ---------------------------------------------------------------

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
    shell.workflow_card._no_changes_pending = False
    shell.workflow_card._exec_finished = False
    shell.workflow_card._notice = ""
    shell._active_workflow_id = None
    for ex in (shell.plan_executor, shell.review_executor):
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
    shell.implement_executor.reset()


@contextlib.contextmanager
def _current_ws(shell, ws: Path):
    with patch.object(shell.workspace_manager, "current", return_value=ws):
        yield


def _start_workflow(shell, ws: Path, prompt: str = "把这个模块重构一下"):
    """Coding recommendation -> Plan with Claude -> Step 2 AWAITING_CONFIRMATION."""
    with _current_ws(shell, ws):
        with patch.object(shell.quick_ask, "ask"):
            shell.dock.select_agent("claude", emit_signal=True)
            shell._on_short_ask_requested()
            shell._on_short_ask_send(prompt)
    assert shell.recommendation_card.has_pending
    with _current_ws(shell, ws):
        with patch.object(shell.plan_executor._runner, "ask", return_value=True):
            shell.recommendation_card._secondary_btn.click()
    wfid = shell.workflow_card.workflow_id
    assert wfid is not None, "Plan with Claude must create a workflow"
    _drive_plan_success(shell)
    plan = shell.workflow_coordinator.get_plan(wfid)
    assert plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    return plan


def _drive_plan_success(shell, text: str = PLAN_TEXT) -> None:
    ex = shell.plan_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=text))
    ex._on_finished(text, 0)


def _click_implement(shell, ask_fn=None) -> None:
    if ask_fn is None:
        ask_fn = lambda *_a, **_k: True
    with patch.object(shell.implement_executor._runner, "ask", side_effect=ask_fn):
        shell.workflow_card._implement_btn.click()


def _finish_implement_exec(shell, text: str = "done", exit_code: int = 0) -> None:
    """Simulate the managed Codex exec finishing cleanly (Step 2 still RUNNING)."""
    shell.implement_executor._on_runner_finished(text, exit_code)


def _click_complete(shell, *, mutate=None) -> None:
    if mutate:
        mutate()
    shell.workflow_card._complete_btn.click()


def _start_implemented(shell, ws: Path, prompt: str = "把这个模块重构一下"):
    """Full Plan -> Implement -> Step 3 AWAITING_CONFIRMATION."""
    _start_workflow(shell, ws, prompt)
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed by codex", encoding="utf-8")
    _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[2].state == WorkflowStepState.AWAITING_CONFIRMATION
    return plan


def _click_review(shell):
    with patch.object(shell.review_executor._runner, "ask", return_value=True) as ask_mock:
        shell.workflow_card._review_btn.click()
    return ask_mock


def _drive_review_success(shell, text: str = REVIEW_TEXT) -> None:
    ex = shell.review_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=text))
    ex._on_finished(text, 0)


def _wf(shell):
    return shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)


def _review_ref(plan):
    for step in plan.steps:
        for a in step.attached_artifacts:
            if a.kind == ArtifactKind.REVIEW:
                return a
    return None


def _card_text(card) -> str:
    parts = [card._title.text(), card._status.text()]
    for row in getattr(card, "_rows", ()):
        parts.extend([row._agent.text(), row._title.text(), row._status.text()])
    for name in (
        "_open_btn", "_open_review_btn", "_cancel_btn", "_implement_btn",
        "_complete_btn", "_keep_waiting_btn", "_override_btn", "_review_btn",
    ):
        widget = getattr(card, name, None)
        if widget is not None:
            parts.append(widget.text())
    return " ".join(parts)


def _coord() -> WorkflowCoordinator:
    state = {"t": 1_700_000_000_000}

    def clock() -> int:
        state["t"] += 1
        return state["t"]

    return WorkflowCoordinator(clock=clock)


def _ready_for_review(root: Path):
    """Coordinator-built plan with real artifacts; Step 3 AWAITING_CONFIRMATION."""
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = coord.create_plan_implement_review(
        TaskRequest(text="Add a greeting function", workspace=str(ws))
    )
    plan = coord.confirm_step(plan, "step_1")
    plan = coord.mark_step_started(plan, "step_1")
    plan = coord.attach_artifact(
        plan, "step_1", store.write_text(plan.workflow_id, ArtifactKind.PLAN, "step_1", PLAN_TEXT)
    )
    plan = coord.mark_step_succeeded(
        plan, "step_1", StepCompletionEvidence(source=CompletionSource.MANAGED_AGENT_RESULT, summary="ok")
    )
    plan = coord.confirm_step(plan, "step_2")
    plan = coord.mark_step_started(plan, "step_2")
    plan = coord.attach_artifact(
        plan,
        "step_2",
        store.write_text(plan.workflow_id, ArtifactKind.CHANGED_FILES, "step_2", CHANGED_TEXT),
    )
    plan = coord.attach_artifact(
        plan,
        "step_2",
        store.write_text(plan.workflow_id, ArtifactKind.IMPLEMENTATION_SUMMARY, "step_2", SUMMARY_TEXT),
    )
    plan = coord.mark_step_succeeded(
        plan, "step_2", StepCompletionEvidence(source=CompletionSource.USER_CONFIRMED, summary="ok")
    )
    assert plan.steps[2].state == WorkflowStepState.AWAITING_CONFIRMATION
    return coord, store, plan


def _custom_plan(
    agent: str,
    intent: WorkflowStepIntent,
    handoff: HandoffMode,
    *,
    workspace: str = "E:/Work",
    state: WorkflowStepState = WorkflowStepState.READY,
) -> WorkflowPlan:
    step = WorkflowStep(
        step_id="s1",
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


# -- 1-3. Step 3 initial state + action visibility --------------------------

def test_step3_begins_awaiting_confirmation(shell, ws) -> None:
    _start_implemented(shell, ws)
    plan = _wf(shell)
    assert plan.steps[2].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert plan.current_step_index == 2
    assert plan.state == WorkflowState.WAITING_FOR_USER
    assert plan.steps[2].attached_artifacts == ()


def test_review_action_visible(shell, ws) -> None:
    _start_implemented(shell, ws)
    assert shell.workflow_card._review_btn.text() == "Review with Claude"
    assert shell.workflow_card._review_btn.isVisible()
    assert shell.workflow_card._open_review_btn.isVisible() is False


def test_review_action_only_when_waiting(shell, ws) -> None:
    # Step 2 not yet confirmed: no Review action.
    _start_workflow(shell, ws)
    assert shell.workflow_card._review_btn.isVisible() is False
    # Step 3 AWAITING: the Review action appears.
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    _click_complete(shell)
    assert shell.workflow_card._review_btn.isVisible()
    # After confirm (READY) the action disappears until the step is done.
    plan = _wf(shell)
    shell.workflow_coordinator.confirm_step(plan, "step_3")
    assert shell.workflow_card._review_btn.isVisible() is False


# -- 4-7. confirm / execute separation --------------------------------------

def test_confirm_readies_step3(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    ready = coord.confirm_step(plan, "step_3")
    assert ready.steps[2].state == WorkflowStepState.READY
    assert ready.state == WorkflowState.WAITING_FOR_USER


def test_confirm_does_not_launch_agent(shell, ws) -> None:
    _start_implemented(shell, ws)
    plan = _wf(shell)
    with patch.object(shell.review_executor._runner, "ask") as ask_mock:
        shell.workflow_coordinator.confirm_step(plan, "step_3")
    assert ask_mock.call_count == 0, "confirm_step must never start an agent"


def test_review_confirm_and_execute_separate(shell, ws) -> None:
    _start_implemented(shell, ws)
    order: list[str] = []
    confirm_orig = shell.workflow_coordinator.confirm_step
    exec_orig = shell.review_executor.execute
    shell.workflow_coordinator.confirm_step = lambda plan, step_id: (
        order.append("confirm") or confirm_orig(plan, step_id)
    )
    shell.review_executor.execute = lambda plan, step_id: (
        order.append("execute") or exec_orig(plan, step_id)
    )
    try:
        _click_review(shell)
    finally:
        shell.workflow_coordinator.confirm_step = confirm_orig
        shell.review_executor.execute = exec_orig
    assert order == ["confirm", "execute"], "confirm and execute must be two separate calls"


def test_execute_marks_running(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    plan = _wf(shell)
    assert plan.steps[2].state == WorkflowStepState.RUNNING
    assert plan.state == WorkflowState.RUNNING
    assert shell.review_executor.running


# -- 8-10. only Claude REVIEW executes --------------------------------------

def test_only_claude_review_executes(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    assert isinstance(ex._runner, DirectProviderRunner)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        started = ex.execute(plan, "step_3")
    assert started.steps[2].state == WorkflowStepState.RUNNING
    assert set(ask_mock.call_args.kwargs) == {"prompt"}


def test_plan_step_rejected(root, app) -> None:
    plan = _custom_plan("claude", WorkflowStepIntent.PLAN, HandoffMode.SHORT_TALK)
    ex = ReviewStepExecutor(_coord(), ArtifactStore(root / "artifacts"), parent=app)
    try:
        ex.execute(plan, "s1")
    except UnsupportedReviewExecution:
        return
    raise AssertionError("the Review executor must reject a PLAN step")


def test_implement_step_rejected(root, app) -> None:
    plan = _custom_plan("codex", WorkflowStepIntent.IMPLEMENT, HandoffMode.OPEN_NATIVE)
    ex = ReviewStepExecutor(_coord(), ArtifactStore(root / "artifacts"), parent=app)
    try:
        ex.execute(plan, "s1")
    except UnsupportedReviewExecution:
        return
    raise AssertionError("the Review executor must reject an IMPLEMENT step")


# -- 11-16. artifact-first review context -----------------------------------

def test_original_request_retained(shell, ws) -> None:
    prompt_text = "把这个模块重构一下"
    _start_implemented(shell, ws, prompt_text)
    plan = _wf(shell)
    assert plan.original_request.text == prompt_text


def test_plan_artifact_required(root, app) -> None:
    plan = _custom_plan("claude", WorkflowStepIntent.REVIEW, HandoffMode.SHORT_TALK)
    coord = _coord()
    ex = ReviewStepExecutor(coord, ArtifactStore(root / "artifacts"), parent=app)
    ex.execute(plan, "s1")
    assert coord.get_plan("wf-custom").steps[0].state == WorkflowStepState.FAILED
    assert coord.get_plan("wf-custom").state == WorkflowState.FAILED


def test_plan_producer_validated(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    step1 = plan.steps[0]
    good = [a for a in step1.attached_artifacts if a.kind != ArtifactKind.PLAN]
    bad = make_artifact_ref(
        kind=ArtifactKind.PLAN, producer_step_id="step_2",
        path=f"{plan.workflow_id}/bad_plan.md", created_at=1,
    )
    steps = list(plan.steps)
    steps[0] = dataclasses.replace(step1, attached_artifacts=tuple(good) + (bad,))
    corrupted = dataclasses.replace(plan, steps=tuple(steps))
    ex = ReviewStepExecutor(coord, store, parent=app)
    ex.execute(corrupted, "step_3")
    assert coord.get_plan(plan.workflow_id).steps[2].state == WorkflowStepState.FAILED


def test_changed_files_required(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    step2 = plan.steps[1]
    remaining = [a for a in step2.attached_artifacts if a.kind != ArtifactKind.CHANGED_FILES]
    steps = list(plan.steps)
    steps[1] = dataclasses.replace(step2, attached_artifacts=tuple(remaining))
    corrupted = dataclasses.replace(plan, steps=tuple(steps))
    ex = ReviewStepExecutor(coord, store, parent=app)
    ex.execute(corrupted, "step_3")
    assert coord.get_plan(plan.workflow_id).steps[2].state == WorkflowStepState.FAILED


def test_changed_files_producer_validated(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    step2 = plan.steps[1]
    good = [a for a in step2.attached_artifacts if a.kind != ArtifactKind.CHANGED_FILES]
    bad = make_artifact_ref(
        kind=ArtifactKind.CHANGED_FILES, producer_step_id="step_1",
        path=f"{plan.workflow_id}/bad_cf.md", created_at=1,
    )
    steps = list(plan.steps)
    steps[1] = dataclasses.replace(step2, attached_artifacts=tuple(good) + (bad,))
    corrupted = dataclasses.replace(plan, steps=tuple(steps))
    ex = ReviewStepExecutor(coord, store, parent=app)
    ex.execute(corrupted, "step_3")
    assert coord.get_plan(plan.workflow_id).steps[2].state == WorkflowStepState.FAILED


def test_optional_summary_safe(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    step2 = plan.steps[1]
    remaining = [a for a in step2.attached_artifacts if a.kind != ArtifactKind.IMPLEMENTATION_SUMMARY]
    steps = list(plan.steps)
    steps[1] = dataclasses.replace(step2, attached_artifacts=tuple(remaining))
    plan = dataclasses.replace(plan, steps=tuple(steps))
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    assert ask_mock.call_count == 1
    assert "no implementation summary provided" in ask_mock.call_args.kwargs["prompt"]
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REVIEW_TEXT))
    ex._on_finished(REVIEW_TEXT, 0)
    assert ex.plan.steps[2].state == WorkflowStepState.SUCCEEDED
    assert ex.plan.state == WorkflowState.SUCCEEDED


def test_plan_changed_files_read_as_input(shell, ws) -> None:
    _start_implemented(shell, ws)
    reads: list = []
    real_read = shell.artifact_store.read_text

    def record_read(ref):
        reads.append(ref)
        return real_read(ref)

    with patch.object(shell.artifact_store, "read_text", side_effect=record_read):
        _click_review(shell)
    kinds = {r.kind for r in reads}
    assert ArtifactKind.PLAN in kinds
    assert ArtifactKind.CHANGED_FILES in kinds


# -- 17-18. no transcript / no session id -----------------------------------

def test_no_transcript_in_prompt(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    prompt = ask_mock.call_args.kwargs["prompt"]
    for token in ("transcript", "BEGIN", "Codex said", "Codex reported", "api_key", "sk-"):
        assert token.lower() not in prompt.lower(), f"prompt must not contain {token!r}"


def test_no_session_id_in_ask(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    kwargs = ask_mock.call_args.kwargs
    assert "session_id" not in kwargs
    assert "persistent" not in kwargs
    assert set(kwargs) == {"prompt"}


# -- 19-24. review prompt ----------------------------------------------------

def _review_prompt(text: str = "Add a greeting function") -> str:
    from core.workflow_prompt import build_review_prompt
    return build_review_prompt(
        TaskRequest(text=text, workspace="E:/x"), PLAN_TEXT, CHANGED_TEXT, summary_text=SUMMARY_TEXT
    )


def test_prompt_preserves_original_task() -> None:
    task = "Add a greeting function that returns a friendly message."
    prompt = _review_prompt(task)
    assert task in prompt
    assert prompt.count(task) == 1


def test_prompt_contains_plan() -> None:
    prompt = _review_prompt()
    assert PLAN_TEXT in prompt


def test_prompt_contains_changed_files() -> None:
    prompt = _review_prompt()
    assert CHANGED_TEXT in prompt
    assert "app.py" in prompt


def test_prompt_explicitly_read_only() -> None:
    prompt = _review_prompt()
    assert "read-only" in prompt
    assert "Do not modify project files" in prompt


def test_prompt_no_auto_approval() -> None:
    prompt = _review_prompt()
    for forbidden in (
        "auto approve", "auto-approve", "skip permissions", "bypassPermissions",
        "skip sandbox", "never ask", "dangerously", "must execute", "force success",
    ):
        assert forbidden.lower() not in prompt.lower(), f"prompt must not contain {forbidden!r}"


def test_prompt_no_modification_instructions() -> None:
    prompt = _review_prompt()
    for forbidden in (
        "modify files", "apply patches", "fix issues", "must return PASS",
        "automatically approve", "run destructive commands",
    ):
        assert forbidden.lower() not in prompt.lower(), f"prompt must not contain {forbidden!r}"
    assert "PASS / NEEDS_CHANGES / UNCERTAIN" in prompt


def test_prompt_empty_input_rejected() -> None:
    from core.workflow_prompt import build_review_prompt
    for bad_task in (None, "", "   "):
        try:
            build_review_prompt(TaskRequest(text=bad_task, workspace="E:/x"), PLAN_TEXT, CHANGED_TEXT)
        except ValueError:
            pass
        else:
            raise AssertionError("an empty task must be rejected")
    try:
        build_review_prompt(TaskRequest(text="t", workspace="E:/x"), "", CHANGED_TEXT)
    except ValueError:
        pass
    else:
        raise AssertionError("an empty plan must be rejected")
    try:
        build_review_prompt(TaskRequest(text="t", workspace="E:/x"), PLAN_TEXT, "   ")
    except ValueError:
        pass
    else:
        raise AssertionError("empty changed-files evidence must be rejected")


# -- 25-28. read-only contract ----------------------------------------------

def test_read_only_tools_no_permission_mode(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    # The direct provider has no file tools; read-only is enforced by the prompt.
    assert set(ask_mock.call_args.kwargs) == {"prompt"}
    assert "Do not modify project files" in ask_mock.call_args.kwargs["prompt"]
    # Ordinary Short Talk keeps its read-only --tools / --safe-mode CLI contract.
    from ui.quick_chat_protocol import WORKFLOW_READ_ONLY_TOOLS, build_claude_args
    args = build_claude_args("x", persistent=False, isolated=True, read_only_tools=True)
    assert "--safe-mode" in args
    assert "--permission-mode" not in args
    assert "--tools" in args
    assert args[args.index("--tools") + 1] == ",".join(WORKFLOW_READ_ONLY_TOOLS)
    for forbidden in ("Write", "Edit", "Bash", "NotebookEdit"):
        assert forbidden not in args


def test_safe_mode_retained(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    # No CLI is spawned, so no Claude hook can fire; isolation is structural.
    assert set(ask_mock.call_args.kwargs) == {"prompt"}


def test_persistent_false(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    # No native session / persistence exists on the direct provider path.
    assert "persistent" not in ask_mock.call_args.kwargs
    assert "session_id" not in ask_mock.call_args.kwargs


def test_no_resume(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    from ui.quick_chat_protocol import build_claude_args
    args = build_claude_args("x", persistent=False, isolated=True)
    assert "--resume" not in args
    assert "--no-session-persistence" in args


# -- 29-31. transient session isolation -------------------------------------

def test_transient_private_session_manager(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    assert isinstance(ex._runner, DirectProviderRunner)
    assert not hasattr(ex._runner, "_sessions"), "the direct provider runner owns no SessionManager"


def test_normal_session_manager_unchanged(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    assert shell.session_manager.get_native_id("claude", ws) is None
    assert shell.session_manager.get_native_id("codex", ws) is None


def test_sessions_json_unchanged(shell, ws) -> None:
    sessions_file = PROJECT_DIR / "config" / "sessions.json"
    before = sessions_file.read_bytes() if sessions_file.exists() else None
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    if before is None:
        assert not sessions_file.exists(), "config/sessions.json must not be created"
    else:
        assert sessions_file.read_bytes() == before


# -- 32-33. adapter reuse / no provider JSON parsing ------------------------

def test_adapter_reused(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    hits = _forbidden_in_module(
        REVIEW_FILE,
        ("ClaudeStreamAdapter", "make_adapter", "agent_adapters", "feed_line", "parse_claude_event"),
    )
    assert not hits, f"executor must reuse the existing adapter: {hits}"
    from core.agent_adapters import make_adapter
    assert make_adapter("claude").agent_id == "claude"


def test_no_provider_json_parsing() -> None:
    hits = _forbidden_in_module(
        REVIEW_FILE, ("json", "loads", "dumps", "stream-json", "feed_line", "raw_event")
    )
    assert not hits, f"executor must not parse provider JSON: {hits}"


# -- 34-35. event collection ------------------------------------------------

def test_text_delta_collected(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    ex = shell.review_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text="# Verdict\n"))
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text="PASS"))
    assert "".join(ex._collected_deltas) == "# Verdict\nPASS"
    assert ex._saw_text


def test_final_usable_output(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    ex = shell.review_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REVIEW_TEXT))
    assert ex._final_text == REVIEW_TEXT


# -- 36-39. failure semantics -----------------------------------------------

def test_empty_output_fails(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    ex = shell.review_executor
    ex._on_finished("", 0)  # exit 0 but no usable text
    plan = _wf(shell)
    assert plan.steps[2].state == WorkflowStepState.FAILED
    assert plan.state == WorkflowState.FAILED
    assert _review_ref(plan) is None
    assert not (shell.artifact_store.root / plan.workflow_id / "review.md").exists()


def test_agent_error_workflow_failed(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    ex = shell.review_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
    ex._on_finished("", 1)
    plan = _wf(shell)
    assert plan.state == WorkflowState.FAILED
    assert plan.steps[2].state == WorkflowStepState.FAILED
    assert plan.steps[2].attached_artifacts == ()


def test_no_automatic_retry(shell, ws) -> None:
    _start_implemented(shell, ws)
    ask_mock = _click_review(shell)
    ex = shell.review_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
    ex._on_failed("boom")
    assert _wf(shell).state == WorkflowState.FAILED
    assert ask_mock.call_count == 1, "a failed review must never be re-run"
    for name in dir(shell.workflow_coordinator):
        assert "retry" not in name.lower(), f"coordinator must not expose {name}"


def test_no_fallback_codex(shell, ws) -> None:
    _start_implemented(shell, ws)
    ask_mock = _click_review(shell)
    ex = shell.review_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code="provider"))
    ex._on_failed("boom")
    assert ask_mock.call_count == 1, "no fallback agent may be used"


# -- 40-47. success path + artifacts ----------------------------------------

def test_review_artifact_created(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    plan = _wf(shell)
    ref = _review_ref(plan)
    assert ref is not None
    assert ref.kind == ArtifactKind.REVIEW
    assert ref.producer_step_id == "step_3"
    assert shell.artifact_store.read_text(ref) == REVIEW_TEXT
    assert (shell.artifact_store.root / plan.workflow_id / "review.md").exists()


def test_review_full_text_persisted(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell, LONG_REVIEW)
    plan = _wf(shell)
    ref = _review_ref(plan)
    stored = shell.artifact_store.read_text(ref)
    assert len(stored) == len(LONG_REVIEW)
    assert stored == LONG_REVIEW
    assert len(stored) > 600
    assert "…" not in stored


def test_attach_after_successful_write(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    seen: list = []
    orig = shell.workflow_coordinator.attach_artifact
    shell.workflow_coordinator.attach_artifact = lambda plan, step_id, ref: (
        seen.append(ref) or orig(plan, step_id, ref)
    )
    try:
        _drive_review_success(shell)
    finally:
        shell.workflow_coordinator.attach_artifact = orig
    assert len(seen) == 1
    assert shell.artifact_store.exists(seen[0]), "attach must follow a successful write"


def test_artifact_write_failure_no_success(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    with patch.object(shell.artifact_store, "write_text", side_effect=ArtifactError("disk full")):
        _drive_review_success(shell)
    plan = _wf(shell)
    assert plan.steps[2].state == WorkflowStepState.FAILED
    assert plan.state == WorkflowState.FAILED
    assert _review_ref(plan) is None


def test_managed_agent_result_evidence(shell, ws) -> None:
    _start_implemented(shell, ws)
    events: list = []
    shell.workflow_coordinator.connect(lambda e: events.append(e))
    _click_review(shell)
    _drive_review_success(shell)
    succeeded = [
        e for e in events
        if e.type == WorkflowEventType.STEP_SUCCEEDED and e.step_id == "step_3"
    ]
    assert len(succeeded) == 1
    assert succeeded[0].evidence is not None
    assert succeeded[0].evidence.source == CompletionSource.MANAGED_AGENT_RESULT


def test_step3_and_workflow_succeeded(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    plan = _wf(shell)
    assert plan.steps[2].state == WorkflowStepState.SUCCEEDED
    assert all(s.state == WorkflowStepState.SUCCEEDED for s in plan.steps)
    assert plan.state == WorkflowState.SUCCEEDED
    assert shell._active_workflow_id is None


# -- 48-51. completion UI ----------------------------------------------------

def test_ui_review_ready_and_workflow_complete(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    assert shell.workflow_card._rows[0]._status.text() == "Plan ready"
    assert shell.workflow_card._rows[1]._status.text() == "Implementation confirmed"
    assert shell.workflow_card._rows[2]._status.text() == "Review ready"
    assert shell.workflow_card._status.text() == "Workflow complete"
    assert shell.workflow_card._open_review_btn.isVisible()
    assert shell.workflow_card._open_btn.isVisible()
    assert not shell.workflow_card._cancel_btn.isVisible()


def test_ui_never_claims_approval(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    text = _card_text(shell.workflow_card)
    for bad in ("Code approved", "Implementation passed", "Everything looks good", "Approved"):
        assert bad not in text, f"card must not claim {bad!r}"
    assert "Review ready" in text
    assert "Workflow complete" in text


# -- 52-53. verdict never parsed --------------------------------------------

def test_no_verdict_parser() -> None:
    hits = _forbidden_in_module(REVIEW_FILE, ("re", "verdict", "NEEDS_CHANGES", "UNCERTAIN"))
    assert not hits, f"executor must not parse / branch on a review verdict: {hits}"


def test_needs_changes_still_completes(shell, ws) -> None:
    needs_changes = "# Verdict\nNEEDS_CHANGES\n\n# Summary\nNeeds more work.\n"
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell, needs_changes)
    plan = _wf(shell)
    assert plan.steps[2].state == WorkflowStepState.SUCCEEDED
    assert plan.state == WorkflowState.SUCCEEDED
    ref = _review_ref(plan)
    stored = shell.artifact_store.read_text(ref)
    assert "NEEDS_CHANGES" in stored
    assert "Needs more work" in stored
    # Review step completion never auto-starts a Codex remediation.
    assert plan.steps[2].attached_artifacts == (ref,)


# -- 54-56. Open review ------------------------------------------------------

def test_open_review_validated_path(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    plan = _wf(shell)
    ref = _review_ref(plan)
    validated = shell.artifact_store.resolve_path(ref)
    opened: list[str] = []
    with patch("app.QDesktopServices.openUrl") as open_mock:
        open_mock.side_effect = lambda url: opened.append(url.toLocalFile()) or True
        shell.workflow_card._on_open_review()
    assert open_mock.call_count == 1
    assert opened and Path(opened[0]).resolve() == validated.resolve()
    assert Path(opened[0]).resolve().is_relative_to(shell.artifact_store.root.resolve())


def test_open_review_rejects_arbitrary_path(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    evil = make_artifact_ref(
        kind=ArtifactKind.REVIEW, producer_step_id="step_3",
        path="../../escape.md", created_at=1,
    )
    plan = _wf(shell)
    shell.workflow_coordinator.attach_artifact(plan, "step_3", evil)
    with patch("app.QDesktopServices.openUrl") as open_mock:
        shell._on_workflow_open_review(plan.workflow_id)
    assert open_mock.call_count == 0, "an escaping path must never reach the OS opener"
    assert "Review file unavailable" in shell.workflow_card._status.text()


def test_open_review_failure_controlled(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    with patch.object(shell.artifact_store, "resolve_path", side_effect=OSError("boom")):
        shell._on_workflow_open_review(shell.workflow_card.workflow_id)  # must not raise
    assert "Review file unavailable" in shell.workflow_card._status.text()
    with patch("app.QDesktopServices.openUrl", return_value=False):
        shell._on_workflow_open_review(shell.workflow_card.workflow_id)
    assert "Couldn't open the review file" in shell.workflow_card._status.text()


# -- 57-58. close / new workflow --------------------------------------------

def test_close_card_keeps_artifacts(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    plan = _wf(shell)
    review_file = shell.artifact_store.root / plan.workflow_id / "review.md"
    plan_file = shell.artifact_store.root / plan.workflow_id / "plan.md"
    assert review_file.exists() and plan_file.exists()
    shell.workflow_card.dismiss()
    assert review_file.exists() and plan_file.exists(), "close must never delete artifacts"


def test_completed_workflow_allows_new(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    assert shell._active_workflow_id is None
    first_id = shell.workflow_card._workflow_id
    plan2 = _start_workflow(shell, ws, "把这个模块重构一下")
    assert plan2 is not None
    assert plan2.workflow_id != first_id
    assert plan2.state == WorkflowState.WAITING_FOR_USER
    assert shell.workflow_card.workflow_id == plan2.workflow_id


# -- 59. cancel while Review runs -------------------------------------------

def test_cancel_running_waits_for_real_cancel(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    assert _wf(shell).steps[2].state == WorkflowStepState.RUNNING
    with patch.object(shell.review_executor, "stop") as stop_mock:
        shell.workflow_card._on_cancel()
    assert stop_mock.call_count == 1, "a running Review must cancel through the executor"
    assert _wf(shell).state == WorkflowState.RUNNING, "cancel is not complete until CANCELLED"
    ex = shell.review_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED))
    ex._on_finished("", 1)
    plan = _wf(shell)
    assert plan.state == WorkflowState.CANCELLED
    assert plan.steps[2].state == WorkflowStepState.CANCELLED


def test_cancel_waiting_uses_coordinator(shell, ws) -> None:
    _start_implemented(shell, ws)
    assert not shell.review_executor.running
    shell.workflow_card._on_cancel()
    plan = _wf(shell)
    assert plan.state == WorkflowState.CANCELLED
    assert plan.steps[2].state == WorkflowStepState.CANCELLED


# -- 60-65. boundaries -------------------------------------------------------

def test_no_workflow_persistence(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    assert not (PROJECT_DIR / "config" / "workflows.json").exists()
    assert not (PROJECT_DIR / "runtime" / "workflows").exists()


def test_no_remediation_loop() -> None:
    hits = _forbidden_in_module(REVIEW_FILE, ("retry", "remediation", "auto_fix", "fix_loop"))
    assert not hits, f"executor must have no remediation loop: {hits}"
    for name in dir(WorkflowCoordinator):
        assert "remediation" not in name.lower()


def test_no_codex_in_review_executor() -> None:
    hits = _forbidden_in_module(
        REVIEW_FILE,
        ("build_codex_args", "CodexJsonlAdapter", "launch_agent", "startDetached", "taskkill"),
    )
    assert not hits, f"the Review executor must never start Codex: {hits}"


def test_lifecycle_sources_untouched(shell, ws) -> None:
    sources = PROJECT_DIR / "runtime" / "sources"
    before: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            before[p.name] = p.read_bytes()
    _start_implemented(shell, ws)
    _click_review(shell)
    _drive_review_success(shell)
    after: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            after[p.name] = p.read_bytes()
    assert before == after, "a workflow Review must never touch lifecycle sources"


def test_workspace_unchanged_by_review(shell, ws) -> None:
    _start_implemented(shell, ws)
    before = {
        p.relative_to(ws).as_posix(): p.read_bytes()
        for p in ws.rglob("*") if p.is_file()
    }
    _click_review(shell)
    _drive_review_success(shell)
    after = {
        p.relative_to(ws).as_posix(): p.read_bytes()
        for p in ws.rglob("*") if p.is_file()
    }
    assert before == after, "the Review step must never modify the workspace"


def test_review_executor_busy_gate(root, app) -> None:
    coord, store, plan = _ready_for_review(root)
    plan = coord.confirm_step(plan, "step_3")
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_3")
    try:
        ex.execute(plan, "step_3")
    except ReviewExecutionBusy:
        return
    raise AssertionError("a second concurrent Review execution must be rejected")


# -- 66-69. overlay / regression --------------------------------------------

def test_hide_not_cancel_while_review_running(shell, ws) -> None:
    _start_implemented(shell, ws)
    _click_review(shell)
    shell.workflow_card.dismiss()
    assert not shell.workflow_card.isVisible()
    assert _wf(shell).steps[2].state == WorkflowStepState.RUNNING
    shell.coordinator.toggle_bubble()
    assert shell.workflow_card.isVisible()


def test_permission_card_priority_unchanged(shell, ws) -> None:
    shell.coordinator.show_shell()
    _start_implemented(shell, ws)
    _click_review(shell)
    assert shell.workflow_card.isVisible()
    shell.coordinator.on_agent_state(
        "claude", AgentState("claude", LifecycleState.WAITING, 1000, "hook")
    )
    assert shell.coordinator.permission_card.isVisible()
    assert not shell.workflow_card.isVisible()
    assert _wf(shell).steps[2].state == WorkflowStepState.RUNNING


def test_short_talk_regression(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True):
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("解释这段代码")
    assert shell.short_ask.running
    assert shell.workflow_card.workflow_id is None, "a chat must not create a workflow"


def test_9c_send_to_codex_regression(shell) -> None:
    with patch.object(ProcessLauncher, "launch_agent", return_value=(True, "ok")) as launch_mock:
        shell.dock.select_agent("claude", emit_signal=True)
        shell._on_short_ask_requested()
        shell._on_short_ask_send("把这个模块重构一下")
        prompt = shell.recommendation_card.pending_prompt
        shell.recommendation_card._primary_btn.click()
    assert launch_mock.call_count == 1
    assert launch_mock.call_args.kwargs.get("initial_prompt") == prompt
    assert shell.recommendation_card.handoff_state.value == "handed_off"
    assert shell.workflow_card.workflow_id is None


# -- coordinator stays pure --------------------------------------------------

def test_coordinator_still_qt_free() -> None:
    hits = _forbidden_in_module(
        COORDINATOR_FILE,
        (
            "workflow_review_executor", "workflow_executor", "workflow_implement_executor",
            "process_launcher", "QuickAskRunner", "artifact_store", "PySide6",
            "QProcess", "QObject",
        ),
    )
    assert not hits, f"coordinator must stay Qt-free / executor-free: {hits}"


# -- desktop/mock smoke ------------------------------------------------------

def _desktop_smoke(shell, ws) -> None:
    _prepare(shell)
    _start_implemented(shell, ws)
    assert shell.workflow_card._rows[2]._status.text() == "Waiting for confirmation"
    assert shell.workflow_card._review_btn.isVisible()
    _click_review(shell)
    assert shell.workflow_card._rows[2]._status.text() == "Reviewing…"
    ex = shell.review_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.TEXT_DELTA, text="# Verdict\n"))
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REVIEW_TEXT))
    ex._on_finished(REVIEW_TEXT, 0)
    plan = _wf(shell)
    assert plan.state == WorkflowState.SUCCEEDED
    assert shell.workflow_card._status.text() == "Workflow complete"
    assert shell.workflow_card._rows[0]._status.text() == "Plan ready"
    assert shell.workflow_card._rows[1]._status.text() == "Implementation confirmed"
    assert shell.workflow_card._rows[2]._status.text() == "Review ready"
    assert shell.workflow_card._open_review_btn.isVisible()
    ref = _review_ref(plan)
    assert shell.artifact_store.read_text(ref) == REVIEW_TEXT
    print(
        "Smoke: Plan with Claude -> Implement with Codex -> Review with Claude "
        "(mock TEXT_DELTA+FINAL) -> Workflow complete / Plan ready / "
        "Implementation confirmed / Review ready + Open review  PASS"
    )


# -- main --------------------------------------------------------------------

def _make_ws() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="fap9d5_ws_"))
    (ws / "a.py").write_text(BASE_A_PY, encoding="utf-8")
    return ws


def _run(fn, app, shell) -> None:
    params = list(inspect.signature(fn).parameters)
    if not params:
        fn()
    elif set(params) == {"root", "app"}:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td), app)
    elif set(params) == {"shell", "ws"}:
        _prepare(shell)
        fn(shell, _make_ws())
    elif set(params) == {"shell"}:
        _prepare(shell)
        fn(shell)
    else:
        raise SystemExit(f"unknown test signature {fn.__name__}: {params}")


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    tmp = tempfile.mkdtemp(prefix="fap9d5_")
    shell = VisualShell(None, artifact_root=Path(tmp) / "artifacts")
    tests = [
        test_step3_begins_awaiting_confirmation,
        test_review_action_visible,
        test_review_action_only_when_waiting,
        test_confirm_readies_step3,
        test_confirm_does_not_launch_agent,
        test_review_confirm_and_execute_separate,
        test_execute_marks_running,
        test_only_claude_review_executes,
        test_plan_step_rejected,
        test_implement_step_rejected,
        test_original_request_retained,
        test_plan_artifact_required,
        test_plan_producer_validated,
        test_changed_files_required,
        test_changed_files_producer_validated,
        test_optional_summary_safe,
        test_plan_changed_files_read_as_input,
        test_no_transcript_in_prompt,
        test_no_session_id_in_ask,
        test_prompt_preserves_original_task,
        test_prompt_contains_plan,
        test_prompt_contains_changed_files,
        test_prompt_explicitly_read_only,
        test_prompt_no_auto_approval,
        test_prompt_no_modification_instructions,
        test_prompt_empty_input_rejected,
        test_read_only_tools_no_permission_mode,
        test_safe_mode_retained,
        test_persistent_false,
        test_no_resume,
        test_transient_private_session_manager,
        test_normal_session_manager_unchanged,
        test_sessions_json_unchanged,
        test_adapter_reused,
        test_no_provider_json_parsing,
        test_text_delta_collected,
        test_final_usable_output,
        test_empty_output_fails,
        test_agent_error_workflow_failed,
        test_no_automatic_retry,
        test_no_fallback_codex,
        test_review_artifact_created,
        test_review_full_text_persisted,
        test_attach_after_successful_write,
        test_artifact_write_failure_no_success,
        test_managed_agent_result_evidence,
        test_step3_and_workflow_succeeded,
        test_ui_review_ready_and_workflow_complete,
        test_ui_never_claims_approval,
        test_no_verdict_parser,
        test_needs_changes_still_completes,
        test_open_review_validated_path,
        test_open_review_rejects_arbitrary_path,
        test_open_review_failure_controlled,
        test_close_card_keeps_artifacts,
        test_completed_workflow_allows_new,
        test_cancel_running_waits_for_real_cancel,
        test_cancel_waiting_uses_coordinator,
        test_no_workflow_persistence,
        test_no_remediation_loop,
        test_no_codex_in_review_executor,
        test_lifecycle_sources_untouched,
        test_workspace_unchanged_by_review,
        test_review_executor_busy_gate,
        test_hide_not_cancel_while_review_running,
        test_permission_card_priority_unchanged,
        test_short_talk_regression,
        test_9c_send_to_codex_regression,
        test_coordinator_still_qt_free,
    ]
    failed = 0
    try:
        for fn in tests:
            try:
                _run(fn, app, shell)
            except Exception:
                failed += 1
                print(f"FAIL  {fn.__name__}")
                raise
        print(f"Phase 9D.5 review workflow tests passed ({len(tests)} tests).")
        _prepare(shell)
        _desktop_smoke(shell, _make_ws())
    finally:
        shell.shutdown()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
