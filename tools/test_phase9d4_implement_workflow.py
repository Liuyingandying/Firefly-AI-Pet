"""Phase 9D.4 — Codex Implement handoff + completion evidence tests.

Covers the required workflow scenarios: Step 2 starts AWAITING_CONFIRMATION;
``Implement with Codex`` confirms Step 2 with confirm and execute as two
separate calls; only READY Codex IMPLEMENT steps execute; a workspace baseline
is captured before the handoff; the PLAN artifact is read and the original
request retained; the implement prompt carries the task + PLAN through the
Phase 9C single-argv transport (no shell); the step is RUNNING before the
handoff and a spawn success leaves it RUNNING (never success); an external
lifecycle success never completes the step; completion requires an explicit
user action that re-scans the workspace, writes CHANGED_FILES +
IMPLEMENTATION_SUMMARY (producer = Step 2), and succeeds only with
USER_CONFIRMED evidence; Step 3 then AWAITS_CONFIRMATION and is never executed;
an empty diff never auto-succeeds and requires an explicit override; double
Implement/complete clicks are blocked; launch failure fails the workflow with
no artifacts; a completion scan failure or a partial artifact write never
falsely succeeds; cancel after handoff is honest (never claims Codex was
killed); SessionManager / sessions.json / PLAN artifact / external lifecycle
stay untouched; the WorkflowCard maps Step-2 states; hide-not-cancel keeps the
baseline; no workflow persistence; no Review; no Codex transcript; no
credentials/secrets in artifacts.

No online calls: Claude Plan and the Codex launch are mocked/fake throughout.
"""

from __future__ import annotations

import contextlib
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
from core.artifact_store import ArtifactError
from core.models import AgentState, LifecycleState
from core.workflow_models import (
    ArtifactKind,
    CompletionSource,
    WorkflowEventType,
    WorkflowState,
    WorkflowStepIntent,
    WorkflowStepState,
)
from core.workspace_snapshot import WorkspaceSnapshotError
from ui.quick_chat_protocol import build_codex_args
from ui.workflow_implement_executor import (
    ImplementExecutionError,
    ImplementStepBusy,
    capture as _real_impl_capture,
)
from test_phase9a_agent_router import _forbidden_in_module

IMPLEMENT_FILE = PROJECT_DIR / "ui" / "workflow_implement_executor.py"
EXECUTOR_CARD_FILE = PROJECT_DIR / "ui" / "workflow_card.py"
COORDINATOR_FILE = PROJECT_DIR / "core" / "workflow_coordinator.py"

PLAN_TEXT = (
    "# Implementation Plan\n\n"
    "- Goal: refactor the auth module\n"
    "- Files: auth.py\n"
    "- Changes: split login flow into smaller functions\n"
)

BASE_A_PY = "def login(): return 1\n"


@contextlib.contextmanager
def _current_ws(shell, ws: Path):
    with patch.object(shell.workspace_manager, "current", return_value=ws):
        yield


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
    shell.implement_executor.reset()


def _drive_plan_success(shell, text: str = PLAN_TEXT) -> None:
    ex = shell.plan_executor
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=text))
    ex._on_finished(text, 0)


def _start_workflow(shell, ws: Path, prompt: str = "把这个模块重构一下"):
    """Coding recommendation -> Plan with Claude -> Step 2 AWAITING_CONFIRMATION."""
    with _current_ws(shell, ws):
        with patch.object(shell.quick_ask, "ask"):
            shell.dock.select_agent("claude", emit_signal=True)
            shell._on_short_ask_requested()
            shell._on_short_ask_send(prompt)
    assert shell.recommendation_card.has_pending
    with _current_ws(shell, ws):
        with patch.object(shell.plan_executor._runner, "ask", return_value=True) as ask_mock:
            shell.recommendation_card._secondary_btn.click()
    wfid = shell.workflow_card.workflow_id
    assert wfid is not None, "Plan with Claude must create a workflow"
    _drive_plan_success(shell)
    plan = shell.workflow_coordinator.get_plan(wfid)
    assert plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    return plan, ask_mock


def _click_implement(shell, ask_fn=None):
    """Click ``Implement with Codex``; the managed runner's ask() is mocked.

    ``ask_fn`` receives (agent, prompt, workspace, **kwargs) and returns a bool.
    """
    if ask_fn is None:
        ask_fn = lambda *_a, **_k: True
    with patch.object(shell.implement_executor._runner, "ask", side_effect=ask_fn) as ask_mock:
        shell.workflow_card._implement_btn.click()
    return ask_mock


def _finish_implement_exec(shell, text: str = "done", exit_code: int = 0) -> None:
    """Simulate the managed Codex exec finishing cleanly (Step 2 still RUNNING)."""
    shell.implement_executor._on_runner_finished(text, exit_code)


def _click_complete(shell, *, mutate=None, override=False):
    if mutate:
        mutate()
    if override:
        shell.workflow_card._override_btn.click()
    else:
        shell.workflow_card._complete_btn.click()


def _wf(shell):
    return shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)


def _attached(plan, kind):
    return [a for s in plan.steps if s.step_id == "step_2" for a in s.attached_artifacts if a.kind == kind]


def _card_text(card) -> str:
    parts = [card._title.text(), card._status.text()]
    for row in getattr(card, "_rows", ()):
        parts.extend([row._agent.text(), row._title.text(), row._status.text()])
    for name in (
        "_primary_btn", "_secondary_btn", "_light_btn", "_open_btn", "_cancel_btn",
        "_implement_btn", "_complete_btn", "_keep_waiting_btn", "_override_btn",
    ):
        widget = getattr(card, name, None)
        if widget is not None:
            parts.append(widget.text())
    return " ".join(parts)


# -- 21-23. Step 2 confirmation ----------------------------------------------

def test_step2_begins_awaiting_confirmation(shell, ws) -> None:
    plan, _ = _start_workflow(shell, ws)
    assert plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert plan.current_step_index == 1
    assert plan.state == WorkflowState.WAITING_FOR_USER
    assert plan.steps[1].attached_artifacts == ()


def test_implement_click_confirms_step(shell, ws) -> None:
    _start_workflow(shell, ws)
    calls: list[str] = []
    original = shell.workflow_coordinator.confirm_step
    shell.workflow_coordinator.confirm_step = lambda plan, step_id: (
        calls.append(step_id) or original(plan, step_id)
    )
    try:
        _click_implement(shell)
    finally:
        shell.workflow_coordinator.confirm_step = original
    assert calls == ["step_2"], "Implement with Codex must confirm exactly Step 2"


def test_confirm_and_execute_separate(shell, ws) -> None:
    _start_workflow(shell, ws)
    order: list[str] = []
    confirm_orig = shell.workflow_coordinator.confirm_step
    exec_orig = shell.implement_executor.execute
    shell.workflow_coordinator.confirm_step = lambda plan, step_id: (
        order.append("confirm") or confirm_orig(plan, step_id)
    )
    shell.implement_executor.execute = lambda plan, step_id: (
        order.append("execute") or exec_orig(plan, step_id)
    )
    try:
        _click_implement(shell)
    finally:
        shell.workflow_coordinator.confirm_step = confirm_orig
        shell.implement_executor.execute = exec_orig
    assert order == ["confirm", "execute"], "confirm and execute must be two separate calls"


# -- 24. READY validation ----------------------------------------------------

def test_only_ready_implement_executes(shell, ws) -> None:
    plan, _ = _start_workflow(shell, ws)
    assert plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    try:
        shell.implement_executor.execute(plan, plan.steps[1].step_id)
    except ImplementExecutionError:
        return
    raise AssertionError("AWAITING_CONFIRMATION step must not execute")


def test_review_step_rejected(shell, ws) -> None:
    plan, _ = _start_workflow(shell, ws)
    # Step 3 (Claude Review) can never be executed by the Implement executor.
    try:
        shell.implement_executor.execute(plan, plan.steps[2].step_id)
    except ImplementExecutionError:
        return
    raise AssertionError("the Implement executor must not run a Review step")


def test_wrong_current_step_rejected(shell, ws) -> None:
    plan, _ = _start_workflow(shell, ws)
    try:
        shell.implement_executor.execute(plan, plan.steps[0].step_id)
    except ImplementExecutionError:
        return
    raise AssertionError("a non-current step must not execute")


# -- 25-28. PLAN + prompt as the only Codex input ----------------------------

def test_baseline_captured_before_launch(shell, ws) -> None:
    _start_workflow(shell, ws)
    order: list[str] = []

    def ordered_capture(root):
        order.append("baseline")
        return _real_impl_capture(root)

    def ordered_ask(*_a, **_k):
        order.append("launch")
        return True

    with patch("ui.workflow_implement_executor.capture", side_effect=ordered_capture), patch.object(
        shell.implement_executor._runner, "ask", side_effect=ordered_ask
    ):
        shell.workflow_card._implement_btn.click()
    assert order == ["baseline", "launch"], "baseline must be captured before the managed exec"


def test_plan_artifact_read_as_input(shell, ws) -> None:
    _start_workflow(shell, ws)
    reads: list = []
    real_read = shell.artifact_store.read_text

    def record_read(ref):
        reads.append(ref)
        return real_read(ref)

    with patch.object(shell.artifact_store, "read_text", side_effect=record_read):
        _click_implement(shell)
    assert len(reads) >= 1, "the executor must read the PLAN artifact"
    assert reads[0].kind == ArtifactKind.PLAN


def test_original_request_retained_in_prompt(shell, ws) -> None:
    prompt_text = "把这个模块重构一下"
    _start_workflow(shell, ws, prompt_text)
    prompts: list[str] = []

    def capture_ask(*_a, **_k):
        prompts.append(_k.get("prompt"))
        return True

    _click_implement(shell, ask_fn=capture_ask)
    assert len(prompts) == 1
    assert prompt_text in prompts[0], "the original task must reach Codex verbatim"


def test_implement_prompt_contains_plan(shell, ws) -> None:
    _start_workflow(shell, ws)
    prompts: list[str] = []

    def capture_ask(*_a, **_k):
        prompts.append(_k.get("prompt"))
        return True

    _click_implement(shell, ask_fn=capture_ask)
    assert PLAN_TEXT in prompts[0], "the PLAN artifact must be the Codex input source"


def test_prompt_has_no_bypass_instructions(shell, ws) -> None:
    from core.workflow_prompt import build_implement_prompt
    from core.routing_models import TaskRequest

    prompt = build_implement_prompt(TaskRequest(text="task", workspace="E:/x"), PLAN_TEXT)
    for forbidden in (
        "auto approve", "auto-approve", "skip permissions", "bypassPermissions",
        "skip sandbox", "never ask", "force success", "dangerously", "must execute",
    ):
        assert forbidden not in prompt.lower(), f"prompt must not contain {forbidden!r}"
    assert "Do not bypass approvals" in prompt
    assert "Work only in the current workspace" in prompt


def test_prompt_empty_input_rejected(shell, ws) -> None:
    from core.workflow_prompt import build_implement_prompt
    from core.routing_models import TaskRequest

    for bad in (None, "", "   "):
        try:
            build_implement_prompt(TaskRequest(text=bad, workspace="E:/x"), PLAN_TEXT)
        except ValueError:
            pass
        else:
            raise AssertionError("an empty task must be rejected")


# -- 29-30. 9C argv transport / no shell -------------------------------------

def test_prompt_uses_managed_exec_transport(shell, ws) -> None:
    """The prompt reaches Codex as one literal argv item via codex exec
    (workspace-write sandbox, JSONL, ephemeral, owned QProcess), never an
    interactive detached handoff."""
    _start_workflow(shell, ws)
    kwargs: dict = {}

    def capture_ask(*_a, **_k):
        kwargs.update(_k)
        return True

    _click_implement(shell, ask_fn=capture_ask)
    assert kwargs["agent"] == "codex"
    assert kwargs["sandbox"] == "workspace-write", "Implement must use the workspace-write sandbox"
    assert kwargs["persistent"] is False, "a workflow Implement must never resume a session"
    args = build_codex_args(kwargs["prompt"], ws, persistent=False, sandbox="workspace-write")
    assert args[0] == "exec"
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert "--json" in args
    assert "--ephemeral" in args
    assert args[-1] == kwargs["prompt"], "the prompt is one literal argv item"


def test_no_shell_in_executor(shell, ws) -> None:
    source = IMPLEMENT_FILE.read_text(encoding="utf-8")
    for forbidden in ("shell=True", "cmd /c", "Invoke-Expression", "Popen", "subprocess", "startDetached", "taskkill", "QProcess"):
        assert forbidden not in source, f"executor must not use {forbidden}"
    hits = _forbidden_in_module(IMPLEMENT_FILE, ("subprocess", "Popen", "startDetached", "taskkill", "QProcess"))
    assert not hits, f"executor must stay shell/QProcess-free: {hits}"


# -- 31-34. RUNNING semantics / lifecycle never authoritative ----------------

def test_mark_running_before_handoff(shell, ws) -> None:
    _start_workflow(shell, ws)
    trace: list = []

    def record(event):
        trace.append(("event", event.type))

    shell.workflow_coordinator.connect(record)

    def fake_ask(*_a, **_k):
        trace.append(("launch", None))
        return True

    _click_implement(shell, ask_fn=fake_ask)
    assert ("event", WorkflowEventType.STEP_STARTED) in trace
    assert trace.index(("event", WorkflowEventType.STEP_STARTED)) < trace.index(("launch", None))


def test_spawn_success_leaves_running(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    assert plan.state == WorkflowState.RUNNING
    assert shell.implement_executor.running


def test_spawn_success_not_success(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    assert plan.steps[1].state != WorkflowStepState.SUCCEEDED
    assert plan.steps[1].attached_artifacts == ()
    assert not (shell.artifact_store.root / plan.workflow_id / "changed_files.md").exists()


def test_external_lifecycle_success_not_success(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    # A lifecycle hook claims Codex SUCCESS; that must never complete Step 2.
    shell.state_monitor.agent_state_changed.emit(
        "codex", AgentState("codex", LifecycleState.SUCCESS, 1_000, "hook")
    )
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    assert plan.steps[1].attached_artifacts == ()
    assert plan.state == WorkflowState.RUNNING
    assert not (shell.artifact_store.root / plan.workflow_id / "changed_files.md").exists()


# -- 35-36. completion scan --------------------------------------------------

def test_completion_requires_user_action(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed by codex", encoding="utf-8")
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING, "no auto-complete after exec finishes"
    assert plan.steps[1].attached_artifacts == ()
    _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED


def test_scan_happens_after_confirmation(shell, ws) -> None:
    _start_workflow(shell, ws)
    calls: list[str] = []

    def counting_capture(root):
        calls.append("scan")
        return _real_impl_capture(root)

    with patch("ui.workflow_implement_executor.capture", side_effect=counting_capture):
        _click_implement(shell)
    assert calls == ["scan"], "baseline scan must run at launch"
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    with patch("ui.workflow_implement_executor.capture", side_effect=counting_capture):
        _click_complete(shell)
    assert calls == ["scan", "scan"], "a second scan must run on user-confirmed completion"


# -- 37-43. artifacts + evidence ---------------------------------------------

def _complete_with_changes(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("modified auth", encoding="utf-8")
    (ws / "new_module.py").write_text("added", encoding="utf-8")
    _click_complete(shell)


def test_changed_files_artifact_produced(shell, ws) -> None:
    _complete_with_changes(shell, ws)
    plan = _wf(shell)
    cf = _attached(plan, ArtifactKind.CHANGED_FILES)
    assert len(cf) == 1
    assert cf[0].producer_step_id == "step_2"
    text = shell.artifact_store.read_text(cf[0])
    assert "## Modified\n- a.py" in text
    assert "## Added\n- new_module.py" in text
    assert "## Deleted\n- (none)" in text


def test_summary_artifact_produced(shell, ws) -> None:
    _complete_with_changes(shell, ws)
    plan = _wf(shell)
    summary = _attached(plan, ArtifactKind.IMPLEMENTATION_SUMMARY)
    assert len(summary) == 1
    assert summary[0].producer_step_id == "step_2"
    text = shell.artifact_store.read_text(summary[0])
    assert "Task:" in text
    assert "把这个模块重构一下" in text
    assert "Plan:" in text
    assert "plan.md" in text
    assert "User confirmed the Codex implementation step completed." in text
    assert "USER_CONFIRMED + WORKSPACE_SNAPSHOT" in text
    assert "- a.py" in text and "- new_module.py" in text


def test_no_transcript_in_summary(shell, ws) -> None:
    _complete_with_changes(shell, ws)
    plan = _wf(shell)
    summary = _attached(plan, ArtifactKind.IMPLEMENTATION_SUMMARY)[0]
    text = shell.artifact_store.read_text(summary)
    for token in ("Codex reported", "Codex said", "transcript", "BEGIN", "session", "reply"):
        assert token not in text, f"summary must not fabricate a transcript: {token!r}"
    assert text.count("User confirmed") == 1


def test_user_confirmed_evidence(shell, ws) -> None:
    _start_workflow(shell, ws)
    events: list = []
    shell.workflow_coordinator.connect(lambda e: events.append(e))
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    _click_complete(shell)
    succeeded = [e for e in events if e.type == WorkflowEventType.STEP_SUCCEEDED and e.step_id == "step_2"]
    assert len(succeeded) == 1
    assert succeeded[0].evidence is not None
    assert succeeded[0].evidence.source == CompletionSource.USER_CONFIRMED


def test_step2_succeeds_only_with_artifacts(shell, ws) -> None:
    _complete_with_changes(shell, ws)
    plan = _wf(shell)
    kinds = {a.kind for a in plan.steps[1].attached_artifacts}
    assert kinds == {ArtifactKind.CHANGED_FILES, ArtifactKind.IMPLEMENTATION_SUMMARY}
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED


def test_step3_awaiting_confirmation_not_executed(shell, ws) -> None:
    _, ask_mock = _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[2].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert plan.current_step_index == 2
    assert plan.state == WorkflowState.WAITING_FOR_USER
    assert plan.steps[2].attached_artifacts == ()
    assert ask_mock.call_count == 1, "only Claude Plan may run via the managed runner"
    assert set(ask_mock.call_args_list[0].kwargs) == {"prompt"}, "no Claude review call"
    assert not (shell.artifact_store.root / plan.workflow_id / "review.md").exists()


# -- 46. Claude managed path zero --------------------------------------------

def test_no_claude_managed_path(shell, ws) -> None:
    _, ask_mock = _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    _click_complete(shell)
    assert ask_mock.call_count == 1
    assert set(ask_mock.call_args_list[0].kwargs) == {"prompt"}
    hits = _forbidden_in_module(
        IMPLEMENT_FILE, ("build_claude_args", "ClaudeStreamAdapter", "make_adapter", "agent_adapters")
    )
    assert not hits, f"implement executor must have no Claude managed path: {hits}"


# -- 47-49. no-changes handling ----------------------------------------------

def test_no_changes_not_auto_succeeded(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    _click_complete(shell)  # no mutation at all
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING, "empty diff must not succeed"
    assert plan.steps[1].attached_artifacts == ()
    assert shell.workflow_card._no_changes_pending
    assert shell.workflow_card._status.text() == "No workspace changes were detected."
    assert shell.workflow_card._keep_waiting_btn.isVisible()
    assert shell.workflow_card._override_btn.isVisible()


def test_keep_waiting(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    _click_complete(shell)
    shell.workflow_card._keep_waiting_btn.click()
    assert not shell.workflow_card._no_changes_pending
    assert shell.workflow_card._complete_btn.isVisible()
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    # A real change afterwards can still complete normally.
    (ws / "a.py").write_text("now changed", encoding="utf-8")
    _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED


def test_explicit_override_completes_empty(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    _click_complete(shell)  # no changes -> keep waiting
    assert not _wf(shell).steps[1].state == WorkflowStepState.SUCCEEDED
    _click_complete(shell, override=True)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED
    cf = _attached(plan, ArtifactKind.CHANGED_FILES)[0]
    text = shell.artifact_store.read_text(cf)
    assert "## Added\n- (none)" in text
    summary = _attached(plan, ArtifactKind.IMPLEMENTATION_SUMMARY)[0]
    assert "- (none)" in shell.artifact_store.read_text(summary)


# -- 50-51. double-action guards ---------------------------------------------

def test_double_implement_launch_blocked(shell, ws) -> None:
    _start_workflow(shell, ws)
    with patch.object(shell.implement_executor._runner, "ask", return_value=True) as ask_mock:
        shell.workflow_card._implement_btn.click()
        shell.workflow_card._implement_btn.click()  # disabled after first click
    assert ask_mock.call_count == 1, "double click must launch only once"
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING


def test_implement_executor_busy_guard(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    plan = _wf(shell)
    try:
        shell.implement_executor.execute(plan, "step_2")
    except ImplementStepBusy:
        return
    raise AssertionError("a second concurrent Implement execution must be rejected")


def test_double_completion_blocked(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED
    assert len(plan.steps[1].attached_artifacts) == 2
    attempt = shell.implement_executor.confirm_completion(plan, "step_2")
    assert not attempt.ok, "a completed execution must not complete again"


# -- 52-55. failure semantics ------------------------------------------------

def test_launch_failure_workflow_failed(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell, ask_fn=lambda *_a, **_k: False)
    plan = _wf(shell)
    assert plan.state == WorkflowState.FAILED
    assert plan.steps[1].state == WorkflowStepState.FAILED
    assert plan.steps[2].state == WorkflowStepState.SKIPPED
    assert not shell.implement_executor.running


def test_no_artifacts_on_launch_failure(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell, ask_fn=lambda *_a, **_k: False)
    plan = _wf(shell)
    assert plan.steps[1].attached_artifacts == ()
    for name in ("changed_files.md", "implementation_summary.md"):
        assert not (shell.artifact_store.root / plan.workflow_id / name).exists()


def test_snapshot_scan_failure_no_false_success(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    with patch(
        "ui.workflow_implement_executor.capture", side_effect=WorkspaceSnapshotError("io boom")
    ):
        _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING, "scan failure must not succeed"
    assert plan.steps[1].attached_artifacts == ()
    assert "Couldn't inspect workspace changes" in shell.workflow_card._status.text()
    # A retry of the completion scan can still finish normally.
    _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED


def test_partial_artifact_write_no_false_success(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    store = shell.artifact_store
    real_write = store.write_text

    def flaky(workflow_id, kind, producer, text):
        if kind == ArtifactKind.IMPLEMENTATION_SUMMARY:
            raise ArtifactError("disk full")
        return real_write(workflow_id, kind, producer, text)

    with patch.object(store, "write_text", side_effect=flaky):
        _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING, "partial write must not succeed"
    assert plan.steps[1].attached_artifacts == ()
    assert not (store.root / plan.workflow_id / "changed_files.md").exists(), (
        "the first artifact must be rolled back"
    )


# -- 56. cancel owns the managed process -------------------------------------

def test_cancel_after_handoff_is_honest(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    assert shell.implement_executor.running
    with patch.object(shell.implement_executor, "stop") as stop_mock:
        shell.workflow_card._on_cancel()
    assert stop_mock.call_count == 1, "cancelling a managed exec must stop the owned process"
    plan = _wf(shell)
    assert plan.state == WorkflowState.CANCELLED
    assert plan.steps[1].state == WorkflowStepState.CANCELLED
    assert plan.steps[2].state == WorkflowStepState.SKIPPED
    assert not shell.implement_executor.running, "executor state is dropped on cancel"
    status = shell.workflow_card._status.text()
    assert "Workflow cancelled." in status
    assert "still running separately" not in status


# -- 57-60. isolation --------------------------------------------------------

def test_session_manager_unchanged(shell, ws) -> None:
    _complete_with_changes(shell, ws)
    assert shell.session_manager.get_native_id("codex", ws) is None
    assert shell.session_manager.get_native_id("claude", ws) is None


def test_sessions_json_unchanged(shell, ws) -> None:
    sessions_file = PROJECT_DIR / "config" / "sessions.json"
    before = sessions_file.read_bytes() if sessions_file.exists() else None
    _complete_with_changes(shell, ws)
    if before is None:
        assert not sessions_file.exists(), "config/sessions.json must not be created"
    else:
        assert sessions_file.read_bytes() == before


def test_plan_artifact_unchanged(shell, ws) -> None:
    plan, _ = _start_workflow(shell, ws)
    plan_md = shell.artifact_store.root / plan.workflow_id / "plan.md"
    before = plan_md.read_bytes()
    _click_implement(shell)
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("changed", encoding="utf-8")
    _click_complete(shell)
    assert plan_md.read_bytes() == before, "the PLAN artifact must stay untouched"


def test_lifecycle_sources_untouched(shell, ws) -> None:
    sources = PROJECT_DIR / "runtime" / "sources"
    before: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            before[p.name] = p.read_bytes()
    _complete_with_changes(shell, ws)
    after: dict[str, bytes] = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            after[p.name] = p.read_bytes()
    assert before == after, "a workflow Implement must never write lifecycle sources"


# -- 61-63. WorkflowCard -----------------------------------------------------

def test_card_state_mappings(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    assert shell.workflow_card._rows[1]._status.text() == "Working in Codex"
    _finish_implement_exec(shell)
    assert shell.workflow_card._rows[1]._status.text() == "Codex finished · Review changes"
    assert shell.workflow_card._complete_btn.isVisible()
    (ws / "a.py").write_text("changed", encoding="utf-8")
    _click_complete(shell)
    assert shell.workflow_card._rows[0]._status.text() == "Plan ready"
    assert shell.workflow_card._rows[1]._status.text() == "Implementation confirmed"
    assert shell.workflow_card._rows[2]._status.text() == "Waiting for confirmation"
    text = _card_text(shell.workflow_card)
    assert "Codex succeeded" not in text, "never claim Codex succeeded on its own"


def test_card_step3_review_action(shell, ws) -> None:
    """9D.5: Step 3 now offers the Review with Claude action (was 9D.4's
    test_card_step3_has_no_review_action, superseded by the review step)."""
    _complete_with_changes(shell, ws)
    assert shell.workflow_card._rows[2]._status.text() == "Waiting for confirmation"
    assert shell.workflow_card._review_btn.text() == "Review with Claude"
    assert shell.workflow_card._review_btn.isVisible(), (
        "Review with Claude must be actionable when Step 3 awaits confirmation"
    )


def test_hide_not_cancel(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    shell.workflow_card.dismiss()
    assert not shell.workflow_card.isVisible()
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    assert shell.implement_executor.running
    shell.coordinator.toggle_bubble()  # Firefly click recovers the card
    assert shell.workflow_card.isVisible()


def test_baseline_retained_while_hidden(shell, ws) -> None:
    _start_workflow(shell, ws)
    _click_implement(shell)
    shell.workflow_card.dismiss()
    (ws / "a.py").write_text("changed while hidden", encoding="utf-8")
    shell.workflow_card.resume_show()
    _finish_implement_exec(shell)
    _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED
    cf = _attached(plan, ArtifactKind.CHANGED_FILES)[0]
    assert "a.py" in shell.artifact_store.read_text(cf), "baseline survived the hide"


# -- 64-67. no persistence / no review / no transcript / no secrets ----------

def test_no_workflow_persistence(shell, ws) -> None:
    _complete_with_changes(shell, ws)
    assert not (PROJECT_DIR / "config" / "workflows.json").exists()
    assert not (PROJECT_DIR / "runtime" / "workflows").exists()


def test_no_codex_transcript_capture(shell, ws) -> None:
    hits = _forbidden_in_module(
        IMPLEMENT_FILE, ("transcript", "readAllStandardOutput", "feed_line", "CodexJsonlAdapter", "stdout", "loads", "dumps", "json")
    )
    assert not hits, f"executor must delegate JSON parsing to the existing adapter: {hits}"


def test_artifacts_contain_no_secrets(shell, ws) -> None:
    _complete_with_changes(shell, ws)
    plan = _wf(shell)
    for ref in plan.steps[1].attached_artifacts:
        text = shell.artifact_store.read_text(ref)
        for bad in ("api_key", "apikey", "sk-", "password", "Authorization", "Bearer "):
            assert bad.lower() not in text.lower(), f"{bad!r} leaked into {ref.kind.value}"
        assert str(ws).lower() not in text.lower(), "no absolute workspace path in artifacts"
    cf = _attached(plan, ArtifactKind.CHANGED_FILES)[0]
    text = shell.artifact_store.read_text(cf)
    for line in text.splitlines():
        if line.startswith("- ") and not line.startswith("- (none)"):
            path = line[2:]
            assert not Path(path).is_absolute()
            assert ".." not in path
    assert "def login" not in text, "CHANGED_FILES must never contain file contents"


# -- coordinator stays pure --------------------------------------------------

def test_coordinator_still_qt_free(shell, ws) -> None:
    hits = _forbidden_in_module(
        COORDINATOR_FILE,
        ("workflow_executor", "workflow_implement_executor", "workspace_snapshot", "process_launcher", "PySide6", "QProcess", "QObject"),
    )
    assert not hits, f"coordinator must stay Qt-free / executor-free: {hits}"


# -- desktop/mock smoke ------------------------------------------------------

def _desktop_smoke(shell, ws) -> None:
    _prepare(shell)
    plan, ask_mock = _start_workflow(shell, ws)
    with patch.object(shell.implement_executor._runner, "ask", return_value=True):
        shell.workflow_card._implement_btn.click()
    assert shell.workflow_card._rows[1]._status.text() == "Working in Codex"
    _finish_implement_exec(shell)
    (ws / "a.py").write_text("smoke change", encoding="utf-8")
    _click_complete(shell)
    plan = _wf(shell)
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED
    assert shell.workflow_card._rows[0]._status.text() == "Plan ready"
    assert shell.workflow_card._rows[1]._status.text() == "Implementation confirmed"
    assert shell.workflow_card._rows[2]._status.text() == "Waiting for confirmation"
    assert ask_mock.call_count == 1
    assert set(ask_mock.call_args_list[0].kwargs) == {"prompt"}
    print(
        "Smoke: Plan with Claude -> Implement with Codex -> mutate workspace -> "
        "Implementation complete -> Claude·Plan Plan ready / Codex·Implement "
        "Implementation confirmed / Claude·Review Waiting for confirmation  PASS"
    )


# -- main --------------------------------------------------------------------

def _make_ws() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="fap9d4_ws_"))
    (ws / "a.py").write_text(BASE_A_PY, encoding="utf-8")
    return ws


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    tmp = tempfile.mkdtemp(prefix="fap9d4_")
    shell = VisualShell(None, artifact_root=Path(tmp) / "artifacts")
    tests = [
        test_step2_begins_awaiting_confirmation,
        test_implement_click_confirms_step,
        test_confirm_and_execute_separate,
        test_only_ready_implement_executes,
        test_review_step_rejected,
        test_wrong_current_step_rejected,
        test_baseline_captured_before_launch,
        test_plan_artifact_read_as_input,
        test_original_request_retained_in_prompt,
        test_implement_prompt_contains_plan,
        test_prompt_has_no_bypass_instructions,
        test_prompt_empty_input_rejected,
        test_prompt_uses_managed_exec_transport,
        test_no_shell_in_executor,
        test_mark_running_before_handoff,
        test_spawn_success_leaves_running,
        test_spawn_success_not_success,
        test_external_lifecycle_success_not_success,
        test_completion_requires_user_action,
        test_scan_happens_after_confirmation,
        test_changed_files_artifact_produced,
        test_summary_artifact_produced,
        test_no_transcript_in_summary,
        test_user_confirmed_evidence,
        test_step2_succeeds_only_with_artifacts,
        test_step3_awaiting_confirmation_not_executed,
        test_no_claude_managed_path,
        test_no_changes_not_auto_succeeded,
        test_keep_waiting,
        test_explicit_override_completes_empty,
        test_double_implement_launch_blocked,
        test_implement_executor_busy_guard,
        test_double_completion_blocked,
        test_launch_failure_workflow_failed,
        test_no_artifacts_on_launch_failure,
        test_snapshot_scan_failure_no_false_success,
        test_partial_artifact_write_no_false_success,
        test_cancel_after_handoff_is_honest,
        test_session_manager_unchanged,
        test_sessions_json_unchanged,
        test_plan_artifact_unchanged,
        test_lifecycle_sources_untouched,
        test_card_state_mappings,
        test_card_step3_review_action,
        test_hide_not_cancel,
        test_baseline_retained_while_hidden,
        test_no_workflow_persistence,
        test_no_codex_transcript_capture,
        test_artifacts_contain_no_secrets,
        test_coordinator_still_qt_free,
    ]
    failed = 0
    try:
        for fn in tests:
            _prepare(shell)
            try:
                fn(shell, _make_ws())
            except Exception:
                failed += 1
                print(f"FAIL  {fn.__name__}")
                raise
        print(f"Phase 9D.4 implement workflow tests passed ({len(tests)} tests).")
        _prepare(shell)
        _desktop_smoke(shell, _make_ws())
    finally:
        shell.shutdown()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
