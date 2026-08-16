"""Phase 9D.6-F1 — Final E2E harness H4-compatibility hotfix tests.

Covers the 20 required scenarios: the old H2 "managed Claude" wait assumption is
gone (1), the harness no longer references QuickAskRunner as the Plan completion
source (2), never launches the Claude CLI (3) or ``run_cli.ps1`` (4), waits on
the current H4 executor's ``turn_finished`` (5), gates Step 1 on the authoritative
WorkflowCoordinator state (6-8), starts Implement only after Step 1 is valid (9),
never treats a finished Codex exec as Step 2 success (10, 11), uses workspace
evidence on ``--continue`` (12-14), routes Review through the current executor /
direct provider (15-17), reaches workflow SUCCEEDED (18), and never auto-confirms
the user (19) or changes production semantics (20).

No online calls: runners are mocked/fake throughout.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEvent, AgentEventType
from core.artifact_store import ArtifactStore
from core.routing_models import TaskRequest
from core.workflow_coordinator import WorkflowCoordinator, WorkflowTransitionError
from core.workflow_models import (
    ArtifactKind,
    CompletionSource,
    StepCompletionEvidence,
    WorkflowEventType,
    WorkflowState,
    WorkflowStepState,
)
from core.workspace_snapshot import capture
from ui.workflow_provider_runner import DirectProviderRunner
from ui.workflow_review_executor import ReviewStepExecutor

import tools.smoke_phase9d6_h2_staged as staged

STAGED_HARNESS = PROJECT_DIR / "tools" / "smoke_phase9d6_h2_staged.py"

D6_TASK = "Fix greeting.py so the existing test passes. Do not modify test_greeting.py."

GOOD_PLAN = (
    "# Plan: Fix greeting.py\n\n"
    "## Goal\nMake `greet(name)` return `\"Hello, {name}!\"` so the existing test passes.\n\n"
    "## Relevant files\n"
    "- greeting.py: change `return \"Hello\"` to `return f\"Hello, {name}!\"`\n"
    "- test_greeting.py: read-only reference; must not be modified\n\n"
    "## Planned changes\n"
    "1. Update greeting.py to interpolate the passed name.\n\n"
    "## Validation\n"
    "- Run `python test_greeting.py` — must pass.\n"
)

REVIEW_TEXT = (
    "# Verdict\nPASS\n\n# Summary\nThe change matches the plan.\n\n"
    "# Findings\nNone.\n\n# Validation\nTests pass.\n\n# Recommended Next Actions\nNone.\n"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _src() -> str:
    return STAGED_HARNESS.read_text(encoding="utf-8")


def _run_plan_and_implement_src() -> str:
    src = _src()
    start = src.index("def run_plan_and_implement")
    end = src.index("def _reconstruct_executor")
    return src[start:end]


def _coord() -> WorkflowCoordinator:
    state = {"t": 1_700_000_000_000}

    def clock() -> int:
        state["t"] += 1
        return state["t"]

    return WorkflowCoordinator(clock=clock)


def _ws(root: Path) -> Path:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _req(ws: Path, text: str = D6_TASK) -> TaskRequest:
    return TaskRequest(text=text, workspace=str(ws))


def _plan_step1_succeeded(coord, store, ws):
    plan = coord.create_plan_implement_review(_req(ws))
    plan = coord.confirm_step(plan, "step_1")
    plan = coord.mark_step_started(plan, "step_1")
    ref = store.write_text(plan.workflow_id, ArtifactKind.PLAN, "step_1", GOOD_PLAN)
    plan = coord.attach_artifact(plan, "step_1", ref)
    plan = coord.mark_step_succeeded(
        plan, "step_1",
        StepCompletionEvidence(source=CompletionSource.MANAGED_AGENT_RESULT, summary="ok"),
    )
    return plan


def _plan_with_review_ready(coord, store, ws):
    plan = _plan_step1_succeeded(coord, store, ws)  # step1 SUCCEEDED, step2 AWAITING
    plan = coord.confirm_step(plan, "step_2")
    plan = coord.mark_step_started(plan, "step_2")
    plan = coord.attach_artifact(
        plan, "step_2",
        store.write_text(plan.workflow_id, ArtifactKind.CHANGED_FILES, "step_2",
                         "# Changed Files\n\n## Modified\n- greeting.py\n"),
    )
    plan = coord.attach_artifact(
        plan, "step_2",
        store.write_text(plan.workflow_id, ArtifactKind.IMPLEMENTATION_SUMMARY, "step_2",
                         "# Implementation Summary\nTask: x\n"),
    )
    plan = coord.mark_step_succeeded(
        plan, "step_2", StepCompletionEvidence(source=CompletionSource.USER_CONFIRMED, summary="ok")
    )
    return coord.confirm_step(plan, "step_3")


def _reconstruct_state(root, app):
    ws = _ws(root)
    (ws / "greeting.py").write_text(staged.FIXTURE_GREETING, encoding="utf-8")
    store = ArtifactStore(root / "artifacts")
    state = {
        "workspace": str(ws),
        "task_text": D6_TASK,
        "plan_text": GOOD_PLAN,
        "baseline": staged.snapshot_to_dict(capture(ws)),
        "codex_exit_code": 0,
        "execution_finished": True,
    }
    coord, store, plan, impl_ex = staged._reconstruct_executor(app, store, state)
    return ws, store, coord, plan, impl_ex


def _run(fn, app):
    params = list(inspect.signature(fn).parameters)
    if not params:
        return fn()
    if params == ["app"]:
        return fn(app)
    if params == ["root", "app"]:
        with tempfile.TemporaryDirectory() as td:
            return fn(Path(td), app)
    raise SystemExit(f"unknown signature {fn.__name__}: {params}")


class _FakeExecutor(QObject):
    turn_finished = Signal()


# ===========================================================================
# 1-5. old wait removed; no QuickAskRunner / Claude CLI / run_cli.ps1
# ===========================================================================

def test_old_managed_claude_wait_removed() -> None:
    src = _src()
    assert "managed Claude" not in src, "the old 'managed Claude' wait wording must be gone"
    assert "_wait_executor" not in src, "the inverted _wait_executor helper must be gone"
    assert "not timer.isActive()" not in src, "the inverted timer check must be gone"
    assert "_wait_for_turn" in src


def test_no_quickaskrunner_reference() -> None:
    src = _src()
    assert "QuickAskRunner" not in src, "the harness must not reference QuickAskRunner"
    assert "process_launcher" not in src, "the harness must not import the process launcher"


def test_no_claude_cli_launch() -> None:
    src = _src()
    for forbidden in ("build_claude_args", "stream-json", "CLI_WRAPPER", "ProcessLauncher", "launch_agent"):
        assert forbidden not in src, f"harness must not launch the Claude CLI: {forbidden!r}"


def test_no_run_cli_ps1() -> None:
    assert "run_cli.ps1" not in _src()


def test_plan_completion_waits_current_executor() -> None:
    src = _src()
    assert "PlanStepExecutor" in src
    assert "ReviewStepExecutor" in src
    assert "turn_finished" in src, "the harness must wait on the executor's turn_finished"


# ===========================================================================
# 5. (behavioral) the event-loop wait is driven by turn_finished
# ===========================================================================

def test_wait_for_turn_true_on_finished() -> None:
    ex = _FakeExecutor()
    QTimer.singleShot(0, lambda: ex.turn_finished.emit())
    assert staged._wait_for_turn(ex, 5000) is True


def test_wait_for_turn_false_on_timeout() -> None:
    ex = _FakeExecutor()
    assert staged._wait_for_turn(ex, 1) is False


# ===========================================================================
# 6-8. Step 1 gate = workflow state, not transport
# ===========================================================================

def test_step1_succeeded_gate_all(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_step1_succeeded(coord, store, ws)
    assert staged._step1_succeeded(plan) is True


def test_step1_succeeded_rejects_none() -> None:
    assert staged._step1_succeeded(None) is False


def test_step1_gate_requires_succeeded(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = coord.create_plan_implement_review(_req(ws))
    plan = coord.confirm_step(plan, "step_1")  # still AWAITING_CONFIRMATION
    assert staged._step1_succeeded(plan) is False


def test_step1_gate_requires_plan_artifact(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_step1_succeeded(coord, store, ws)
    stripped = dataclasses.replace(
        plan,
        steps=(dataclasses.replace(plan.steps[0], attached_artifacts=()),) + plan.steps[1:],
    )
    assert stripped.steps[0].state == WorkflowStepState.SUCCEEDED
    assert staged._step1_succeeded(stripped) is False, "a missing PLAN artifact must fail the gate"


def test_step1_gate_requires_step2_awaiting(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_step1_succeeded(coord, store, ws)
    mutated = dataclasses.replace(
        plan,
        steps=(plan.steps[0], dataclasses.replace(plan.steps[1], state=WorkflowStepState.PENDING))
        + plan.steps[2:],
    )
    assert staged._step1_succeeded(mutated) is False, "Step 2 must be AWAITING_CONFIRMATION"


def test_step1_gate_requires_workflow_waiting(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_step1_succeeded(coord, store, ws)
    mutated = dataclasses.replace(plan, state=WorkflowState.RUNNING)
    assert staged._step1_succeeded(mutated) is False, "the workflow must be WAITING_FOR_USER"


# ===========================================================================
# 9. Implement is gated after Step 1 validity
# ===========================================================================

def test_implement_gated_after_step1_valid() -> None:
    src = _src()
    gate = src.index("if not _step1_succeeded(plan):")
    step2_confirm = src.index('confirm_step(plan, "step_2")')
    impl = src.index("ImplementStepExecutor(")
    assert gate < step2_confirm < impl, "Step 2 must only be confirmed/executed after the Step 1 gate"


# ===========================================================================
# 10-11, 19. exec finished != success; Step 2 RUNNING before --continue
# ===========================================================================

def test_codex_finish_not_step2_success() -> None:
    run_src = _run_plan_and_implement_src()
    assert "confirm_completion" not in run_src, "run-plan-and-implement must never auto-complete Step 2"
    assert "mark_step_succeeded" not in run_src
    assert "USER_CONFIRMED" not in run_src
    assert "managed_exec_finished" in run_src, "exec-finished is recorded as a fact, not as success"


def test_no_auto_user_confirmation() -> None:
    src = _src()
    run_src = _run_plan_and_implement_src()
    assert "confirm_completion" not in run_src
    cont_start = src.index("def continue_stage")
    assert "confirm_completion" in src[cont_start:], "only --continue may confirm completion"


def test_step2_running_before_continue(root, app) -> None:
    ws, store, coord, plan, impl_ex = _reconstruct_state(root, app)
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    assert plan.state == WorkflowState.RUNNING
    assert impl_ex.managed_exec_finished is True
    assert impl_ex.managed_exec_exit_code == 0


# ===========================================================================
# 12-14. --continue uses workspace evidence; both artifacts required
# ===========================================================================

def test_continue_uses_workspace_evidence(root, app) -> None:
    ws, store, coord, plan, impl_ex = _reconstruct_state(root, app)
    (ws / "greeting.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
    attempt = impl_ex.confirm_completion(plan, "step_2")
    assert attempt.ok, attempt.message
    plan = impl_ex.plan
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED
    kinds = {a.kind for a in plan.steps[1].attached_artifacts}
    assert kinds == {ArtifactKind.CHANGED_FILES, ArtifactKind.IMPLEMENTATION_SUMMARY}
    assert plan.steps[2].state == WorkflowStepState.AWAITING_CONFIRMATION


def test_changed_files_required_no_empty(root, app) -> None:
    ws, store, coord, plan, impl_ex = _reconstruct_state(root, app)
    attempt = impl_ex.confirm_completion(plan, "step_2")
    assert attempt.ok is False
    assert attempt.no_changes is True
    assert impl_ex.plan.steps[1].state == WorkflowStepState.RUNNING, "an empty diff must not succeed Step 2"


# ===========================================================================
# 15-18. Review routes through the current executor / direct provider
# ===========================================================================

def test_review_uses_current_review_executor() -> None:
    src = _src()
    assert "ReviewStepExecutor" in src
    assert "build_claude_args" not in src
    assert "QuickAskRunner" not in src


def test_review_direct_provider_path(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex = ReviewStepExecutor(coord, store, parent=app)
    assert isinstance(ex._runner, DirectProviderRunner)


def test_review_artifact_required() -> None:
    src = _src()
    assert "ArtifactKind.REVIEW" in src
    assert 'find_attached(plan, "step_3", ArtifactKind.REVIEW)' in src


def test_review_artifact_and_workflow_succeeded(root, app) -> None:
    ws = _ws(root)
    (ws / "greeting.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_3")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REVIEW_TEXT))
    ex._on_finished(REVIEW_TEXT, 0)
    plan = ex.plan
    assert staged.find_attached(plan, "step_3", ArtifactKind.REVIEW) is not None
    assert plan.steps[2].state == WorkflowStepState.SUCCEEDED
    assert plan.state == WorkflowState.SUCCEEDED


# ===========================================================================
# 20. production semantics unchanged
# ===========================================================================

def test_no_production_semantics_modified() -> None:
    assert {s.value for s in CompletionSource} == {
        "managed_agent_result", "user_confirmed", "artifact_validated",
    }, "CompletionSource must keep no lifecycle/observer member"
    assert "workflow_succeeded" in {e.value for e in WorkflowEventType}


def test_codex_step_rejects_managed_agent_result(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = coord.create_plan_implement_review(_req(ws))
    plan = coord.confirm_step(plan, "step_1")
    plan = coord.mark_step_started(plan, "step_1")
    plan = coord.attach_artifact(
        plan, "step_1", store.write_text(plan.workflow_id, ArtifactKind.PLAN, "step_1", GOOD_PLAN)
    )
    plan = coord.mark_step_succeeded(
        plan, "step_1", StepCompletionEvidence(source=CompletionSource.MANAGED_AGENT_RESULT, summary="ok")
    )
    plan = coord.confirm_step(plan, "step_2")
    plan = coord.mark_step_started(plan, "step_2")
    plan = coord.attach_artifact(
        plan, "step_2",
        store.write_text(plan.workflow_id, ArtifactKind.CHANGED_FILES, "step_2", "# Changed Files\n"),
    )
    plan = coord.attach_artifact(
        plan, "step_2",
        store.write_text(plan.workflow_id, ArtifactKind.IMPLEMENTATION_SUMMARY, "step_2", "# Summary\n"),
    )
    try:
        coord.mark_step_succeeded(
            plan, "step_2",
            StepCompletionEvidence(source=CompletionSource.MANAGED_AGENT_RESULT, summary="boom"),
        )
    except WorkflowTransitionError:
        return
    raise AssertionError("MANAGED_AGENT_RESULT must never complete the Codex IMPLEMENT step")


# -- main --------------------------------------------------------------------

def main() -> None:
    app = QApplication.instance() or QApplication([])
    tests = [
        test_old_managed_claude_wait_removed,
        test_no_quickaskrunner_reference,
        test_no_claude_cli_launch,
        test_no_run_cli_ps1,
        test_plan_completion_waits_current_executor,
        test_wait_for_turn_true_on_finished,
        test_wait_for_turn_false_on_timeout,
        test_step1_succeeded_gate_all,
        test_step1_succeeded_rejects_none,
        test_step1_gate_requires_succeeded,
        test_step1_gate_requires_plan_artifact,
        test_step1_gate_requires_step2_awaiting,
        test_step1_gate_requires_workflow_waiting,
        test_implement_gated_after_step1_valid,
        test_codex_finish_not_step2_success,
        test_no_auto_user_confirmation,
        test_step2_running_before_continue,
        test_continue_uses_workspace_evidence,
        test_changed_files_required_no_empty,
        test_review_uses_current_review_executor,
        test_review_direct_provider_path,
        test_review_artifact_required,
        test_review_artifact_and_workflow_succeeded,
        test_no_production_semantics_modified,
        test_codex_step_rejects_managed_agent_result,
    ]
    failed = 0
    for fn in tests:
        try:
            _run(fn, app)
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            raise
    print(f"Phase 9D.6-F1 harness hotfix tests passed ({len(tests)} tests).")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
