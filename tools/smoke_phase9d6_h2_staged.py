"""Phase 9D.6-H2 — Staged, resumable controlled real end-to-end smoke harness.

Replaces the 9D.6 ``--run`` single-pass human gate (which relied on an infinite
background wait for a control marker and was killed by the environment after
~26 min). The D.6 retest is now three independent, resumable stages; each stage
runs to a natural stopping point and exits safely with no blocking background
loop:

    --prepare [--workspace PATH]
        Create the disposable fixture (greeting.py / test_greeting.py /
        README.md) and verify the initial ``python test_greeting.py`` FAILS.

    --run-plan-and-implement --workspace PATH [--state PATH] [--evidence PATH]
        Real Plan through the direct provider (PlanStepExecutor ->
        DirectProviderRunner -> WorkflowCoordinator) -> Step 1 SUCCEEDED ->
        confirm Step 2 -> managed ``codex exec`` (owned process, workspace-write
        sandbox) -> wait for the exec to finish -> save minimal smoke state
        (test-only control JSON) -> exit. The workflow stays RUNNING; the user
        then inspects the workspace.

    --continue [--state PATH] [--evidence PATH] [--review] [--no-cleanup]
        Reconstruct the smoke context from the control JSON, run the real
        completion path (workspace diff + USER_CONFIRMED + artifacts -> Step 2
        SUCCEEDED), run the local test, and optionally run the Review step
        through the direct provider (ReviewStepExecutor -> DirectProviderRunner).
        Writes the evidence JSON.

    --cleanup --workspace PATH
        Remove a disposable smoke workspace (only ever removes a directory
        under the system temp dir, never production).

The control state lives in ``runtime/e2e/`` (test-only, explicitly separate
from production workflow persistence). It holds only allowlisted fields:
workspace path, workflow id, task text, plan text, step states, call counters,
baseline snapshot data, and artifact references. It never holds API keys, full
agent transcripts, or Claude session ids.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from core.artifact_store import ArtifactStore
from core.routing_models import TaskRequest
from core.workflow_coordinator import WorkflowCoordinator
from core.workflow_models import (
    ArtifactKind,
    CompletionSource,
    StepCompletionEvidence,
    WorkflowState,
    WorkflowStepState,
)
from core.workspace_snapshot import FileRecord, Snapshot, capture, diff
from ui.workflow_executor import PlanStepExecutor
from ui.workflow_implement_executor import ImplementStepExecutor
from ui.workflow_review_executor import ReviewStepExecutor

ARTIFACT_ROOT = PROJECT_DIR / "runtime" / "artifacts"
STATE_DIR = PROJECT_DIR / "runtime" / "e2e"
STATE_FILE_DEFAULT = STATE_DIR / "phase9d6_h2_state.json"
EVIDENCE_FILE_DEFAULT = STATE_DIR / "phase9d6_h2_evidence.json"
PLAN_TIMEOUT_MS = 360_000  # 6 minutes per real Claude call
CODEX_TIMEOUT_MS = 600_000  # 10 minutes; the H1 exec smoke took ~144 s
REVIEW_TIMEOUT_MS = 360_000

TASK = "Fix greeting.py so the existing test passes. Do not modify test_greeting.py."
BUSINESS_FILES = ("greeting.py", "test_greeting.py", "README.md")

FIXTURE_GREETING = 'def greet(name):\n    return "Hello"\n'
FIXTURE_TEST = 'from greeting import greet\n\nassert greet("Firefly") == "Hello, Firefly!"\n'
FIXTURE_README = "Tiny disposable Phase 9D.6-H2 test workspace.\n"

# Only these top-level fields are ever written to the test-only control JSON.
ALLOWED_STATE_FIELDS = frozenset(
    {
        "workspace",
        "workflow_id",
        "task_text",
        "plan_text",
        "plan_ref_path",
        "step1_state",
        "step2_state",
        "step3_state",
        "workflow_state",
        "baseline",
        "agent_counts",
        "evidence_path",
        "created_at",
        "codex_exit_code",
        "execution_finished",
    }
)


def log(msg: str) -> None:
    print(msg, flush=True)


def errlog(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# workspace helpers
# ---------------------------------------------------------------------------

def ws_snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            try:
                out[rel] = p.read_bytes()
            except OSError:
                out[rel] = b"<unreadable>"
    return out


def business_changes(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    return [name for name in BUSINESS_FILES if before.get(name) != after.get(name)]


def run_python_test(workspace: Path) -> dict:
    cmd = [sys.executable, str(workspace / "test_greeting.py")]
    try:
        proc = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ran": False, "exit": None, "passed": None, "output_tail": f"<error: {exc}>"}
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
    return {
        "ran": True,
        "exit": proc.returncode,
        "passed": proc.returncode == 0,
        "output_tail": "\n".join(tail[-6:]),
    }


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verdict_from_review(text: str) -> str:
    m = re.search(r"^\s*#+\s*Verdict\s*[:\-]?\s*(PASS|NEEDS_CHANGES|UNCERTAIN)\b", text, re.M | re.I)
    if m:
        return m.group(1).upper()
    m2 = re.search(r"\b(PASS|NEEDS_CHANGES|UNCERTAIN)\b", text[:600])
    if m2:
        return m2.group(1).upper()
    return "not_clearly_present"


def find_attached(plan, step_id: str, kind: ArtifactKind):
    for step in plan.steps:
        if step.step_id == step_id:
            for ref in step.attached_artifacts:
                if ref.kind == kind:
                    return ref
    return None


def _step1_succeeded(plan) -> bool:
    """The authoritative Step 1 completion gate (workflow state, not transport).

    True only when the WorkflowCoordinator immutable plan state shows Step 1
    SUCCEEDED with its PLAN artifact attached AND the plan reached the expected
    post-Plan gate: Step 2 AWAITING_CONFIRMATION and the workflow
    WAITING_FOR_USER. A provider HTTP 200 / AgentEvent FINAL / finished process
    is never, by itself, success.
    """
    if plan is None:
        return False
    return (
        plan.steps[0].state == WorkflowStepState.SUCCEEDED
        and plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
        and plan.state == WorkflowState.WAITING_FOR_USER
        and find_attached(plan, "step_1", ArtifactKind.PLAN) is not None
    )


def _describe_workflow(plan, step_index: int = 0) -> str:
    """One-line snapshot of the authoritative workflow/step state for reports."""
    if plan is None:
        return "workflow_state=<none>"
    step_state = plan.steps[step_index].state.value if step_index < len(plan.steps) else "?"
    return f"workflow_state={plan.state.value} step{step_index + 1}_state={step_state}"


# ---------------------------------------------------------------------------
# test-only control state (allowlisted, no secrets)
# ---------------------------------------------------------------------------

def snapshot_to_dict(snap: Snapshot) -> dict:
    records = {
        p: [r.relative_path, r.size, r.mtime_ns, r.sha256, r.strategy]
        for p, r in snap.records.items()
    }
    return {"root": str(snap.root), "records": records, "skipped": list(snap.skipped)}


def snapshot_from_dict(data: dict) -> Snapshot:
    records = {}
    for p, vals in (data.get("records") or {}).items():
        rel, size, mtime_ns, digest, strategy = vals
        records[p] = FileRecord(rel, int(size), int(mtime_ns), digest, strategy)
    return Snapshot(
        root=Path(data.get("root") or os.getcwd()),
        records=records,
        skipped=tuple(data.get("skipped") or ()),
    )


def save_state(state: dict, path: Path) -> None:
    """Write only allowlisted fields to the test-only control JSON.

    Anything else (API keys, transcripts, session ids) is silently dropped:
    this file must never become a secrets sink.
    """
    allowed = {k: v for k, v in state.items() if k in ALLOWED_STATE_FIELDS}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(allowed, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_evidence(evidence: dict, evidence_arg: str | None, ws: Path) -> None:
    path = Path(evidence_arg) if evidence_arg else (ws.parent / "phase9d6_h2_evidence.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[9D6-H2] evidence written: {path}")


# ---------------------------------------------------------------------------
# stage 1: prepare
# ---------------------------------------------------------------------------

def prepare(workspace_arg: str | None) -> int:
    if workspace_arg:
        ws = Path(workspace_arg).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
    else:
        ws = Path(tempfile.mkdtemp(prefix="firefly_phase9d6_h2_"))
    if (ws / "greeting.py").exists():
        errlog(f"workspace {ws} already has greeting.py; refusing to overwrite fixture")
        return 1
    (ws / "greeting.py").write_text(FIXTURE_GREETING, encoding="utf-8")
    (ws / "test_greeting.py").write_text(FIXTURE_TEST, encoding="utf-8")
    (ws / "README.md").write_text(FIXTURE_README, encoding="utf-8")

    result = run_python_test(ws)
    log(f"[9D6-H2] prepared workspace: {ws}")
    log(f"[9D6-H2] initial test exit={result['exit']} passed={result['passed']}")
    if not result["ran"] or result["passed"] is not False:
        errlog("INITIAL FIXTURE INVALID: python test_greeting.py must FAIL on a fresh fixture.")
        return 1
    log("[9D6-H2] initial test FAILs as required (greet() does not yet return 'Hello, Firefly!')")
    return 0


# ---------------------------------------------------------------------------
# stage 2: real Plan -> managed codex exec -> save state (no blocking gate)
# ---------------------------------------------------------------------------

def _wait_for_turn(executor, timeout_ms: int) -> bool:
    """Drive the Qt event loop until the executor's managed turn ends.

    Returns True when the executor emitted ``turn_finished`` before the timeout,
    False when the timeout fired first. This helper only observes the executor's
    public ``turn_finished`` signal — the H4 direct-provider completion boundary.
    It never reads the Short Talk process runner, the Claude CLI, or the provider
    HTTP response; success/failure is decided by the caller from ``executor.plan``
    (WorkflowCoordinator state), not by this helper.
    """
    finished = {"value": False}

    def _mark_finished() -> None:
        finished["value"] = True

    loop = QEventLoop()
    executor.turn_finished.connect(_mark_finished)
    executor.turn_finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec()
    executor.turn_finished.disconnect(_mark_finished)
    executor.turn_finished.disconnect(loop.quit)
    return finished["value"]


def run_plan_and_implement(
    workspace_arg: str,
    state_arg: str | None,
    evidence_arg: str | None,
    plan_timeout_ms: int,
    codex_timeout_ms: int,
) -> int:
    ws = Path(workspace_arg).expanduser().resolve()
    if not (ws / "greeting.py").is_file():
        errlog(f"workspace {ws} is not prepared (no greeting.py). Run --prepare first.")
        return 1
    if run_python_test(ws)["passed"] is not False:
        errlog("INITIAL FIXTURE INVALID: python test_greeting.py must FAIL before Step 1.")
        return 1

    state_path = Path(state_arg) if state_arg else STATE_FILE_DEFAULT
    evidence: dict = {}
    t_start = time.time()
    app = QApplication.instance() or QApplication([])
    store = ArtifactStore(ARTIFACT_ROOT)
    coord = WorkflowCoordinator()

    ws_before_plan = ws_snapshot(ws)
    req = TaskRequest(text=TASK, workspace=str(ws))
    plan = coord.create_plan_implement_review(req)
    agent_counts = {"claude_plan": 1, "codex_implement": 1, "claude_review": 0}
    evidence["workflow_id_prefix"] = plan.workflow_id[:8]
    evidence["workspace"] = str(ws)
    evidence["task_text"] = TASK

    # ---- Step 1: Claude Plan via the direct provider ----------------------
    log(f"[9D6-H2] workflow={plan.workflow_id} created; confirming Step 1")
    plan = coord.confirm_step(plan, "step_1")
    plan_ex = PlanStepExecutor(coord, store, parent=app)
    agent_events: list[str] = []
    workflow_events: list[str] = []
    plan_ex.agent_event.connect(lambda ev: agent_events.append(ev.type.value))
    coord.connect(lambda ev: workflow_events.append(ev.type.value))
    plan_ex.execute(plan, "step_1")
    if not _wait_for_turn(plan_ex, plan_timeout_ms):
        errlog("[9D6-H2] Step 1 timed out waiting for Direct Provider Plan completion.")
        errlog(f"[9D6-H2] current state: {_describe_workflow(plan_ex.plan, 0)}")
        errlog(f"[9D6-H2] observed WorkflowEvents: {workflow_events or '<none>'}")
        errlog(f"[9D6-H2] observed AgentEvents: {agent_events or '<none>'}")
        return 1
    plan = plan_ex.plan
    if not _step1_succeeded(plan):
        errlog(
            "[9D6-H2] Step 1 did not reach the Plan completion gate "
            f"({_describe_workflow(plan, 0)})."
        )
        return 1
    plan_ref = find_attached(plan, "step_1", ArtifactKind.PLAN)
    plan_text = store.read_text(plan_ref)
    evidence["plan_sha256_prefix"] = (plan_ref.metadata or {}).get("sha256", "")[:12]
    evidence["plan_excerpt"] = plan_text[:800]
    log("[9D6-H2] Step 1 SUCCEEDED (PLAN artifact attached; Step 2 AWAITING_CONFIRMATION)")

    # ---- Step 2: user confirm + managed codex exec ------------------------
    plan = coord.confirm_step(plan, "step_2")
    impl_ex = ImplementStepExecutor(coord, store, parent=app)
    plan = impl_ex.execute(plan, "step_2")
    if plan.steps[1].state != WorkflowStepState.RUNNING:
        errlog("[9D6-H2] Step 2 did not start RUNNING; managed exec launch failed.")
        return 1
    log("[9D6-H2] Step 2 RUNNING: managed codex exec started (owned process)")
    finished_ok = _wait_for_turn(impl_ex, codex_timeout_ms)
    plan = impl_ex.plan
    if not finished_ok or plan is None or plan.state == WorkflowState.FAILED:
        errlog("[9D6-H2] Step 2 managed codex exec did not finish cleanly.")
        return 1
    if not impl_ex.managed_exec_finished:
        errlog("[9D6-H2] Step 2 managed codex exec finished but not marked finished.")
        return 1

    evidence["codex_exit_code"] = impl_ex.managed_exec_exit_code
    evidence["step2_execution_finished"] = True
    log(f"[9D6-H2] Step 2 managed exec finished (exit {impl_ex.managed_exec_exit_code}); still RUNNING, waiting for user confirmation")

    # Save test-only state so --continue can reconstruct without the workflow
    # having persisted anywhere in production.
    state = {
        "workspace": str(ws),
        "workflow_id": plan.workflow_id,
        "task_text": TASK,
        "plan_text": plan_text,
        "plan_ref_path": plan_ref.path,
        "step1_state": plan.steps[0].state.value,
        "step2_state": plan.steps[1].state.value,
        "step3_state": plan.steps[2].state.value,
        "workflow_state": plan.state.value,
        "baseline": snapshot_to_dict(impl_ex._baseline),
        "agent_counts": agent_counts,
        "evidence_path": evidence_arg or str(ws.parent / "phase9d6_h2_evidence.json"),
        "created_at": int(time.time() * 1000),
        "codex_exit_code": impl_ex.managed_exec_exit_code,
        "execution_finished": True,
    }
    save_state(state, state_path)
    log(f"[9D6-H2] smoke state saved: {state_path}")

    ws_after = ws_snapshot(ws)
    evidence["plan_phase_workspace"] = business_changes(ws_before_plan, ws_after)
    write_evidence(evidence, evidence_arg, ws)
    log("[9D6-H2] RESULT=PASS stage (Plan + managed Codex exec). Inspect the workspace, then run --continue.")
    return 0


# ---------------------------------------------------------------------------
# stage 3: --continue (user-confirmed completion + local test [+ Review])
# ---------------------------------------------------------------------------

def _reconstruct_executor(app, store, state: dict):
    """Rebuild coordinator + ImplementStepExecutor at the RUNNING post-exec point."""
    ws = Path(state["workspace"]).expanduser().resolve()
    coord = WorkflowCoordinator()
    req = TaskRequest(text=state["task_text"], workspace=str(ws))
    plan = coord.create_plan_implement_review(req)
    # Replay Step 1 success with the saved plan artifact (real transitions).
    plan = coord.confirm_step(plan, "step_1")
    plan = coord.mark_step_started(plan, "step_1")
    ref = store.write_text(
        plan.workflow_id, ArtifactKind.PLAN, "step_1", state["plan_text"]
    )
    plan = coord.attach_artifact(plan, "step_1", ref)
    plan = coord.mark_step_succeeded(
        plan,
        "step_1",
        StepCompletionEvidence(
            source=CompletionSource.MANAGED_AGENT_RESULT,
            summary="Plan replayed from 9D.6-H2 smoke state",
        ),
    )
    plan = coord.confirm_step(plan, "step_2")
    plan = coord.mark_step_started(plan, "step_2")
    impl_ex = ImplementStepExecutor(coord, store, parent=app)
    # Restore the executor's post-exec state so the real completion path runs.
    impl_ex._plan = plan
    impl_ex._workflow_id = plan.workflow_id
    impl_ex._step_id = "step_2"
    impl_ex._baseline = snapshot_from_dict(state["baseline"])
    impl_ex._completion_attempted = False
    impl_ex._execution_finished = bool(state.get("execution_finished"))
    impl_ex._execution_exit_code = state.get("codex_exit_code")
    impl_ex._handled = True
    impl_ex._busy = True
    return coord, store, plan, impl_ex


def continue_stage(state_arg: str | None, evidence_arg: str | None, review: bool, review_timeout_ms: int) -> int:
    state_path = Path(state_arg) if state_arg else STATE_FILE_DEFAULT
    state = load_state(state_path)
    if not state.get("workspace"):
        errlog(f"no smoke state at {state_path}; run --run-plan-and-implement first.")
        return 1
    ws = Path(state["workspace"]).expanduser().resolve()
    if not (ws / "greeting.py").is_file():
        errlog(f"workspace {ws} is missing; cannot continue.")
        return 1

    evidence: dict = dict(state)
    issues: list[str] = []
    app = QApplication.instance() or QApplication([])
    store = ArtifactStore(ARTIFACT_ROOT)
    coord, store, plan, impl_ex = _reconstruct_executor(app, store, state)

    # ---- Step 2 completion (user-confirmed, production path) --------------
    attempt = impl_ex.confirm_completion(plan, "step_2")
    plan = impl_ex.plan or plan
    evidence["step2_completion"] = {
        "ok": attempt.ok,
        "no_changes": attempt.no_changes,
        "snapshot_error": attempt.snapshot_error,
        "message": attempt.message,
    }
    if not attempt.ok:
        issues.append(f"Step 2 completion not accepted: {attempt.message}")
        evidence["issues"] = issues
        write_evidence(evidence, evidence_arg, ws)
        log("[9D6-H2] RESULT=FAIL (Step 2 completion rejected; no Review)")
        return 1
    log("[9D6-H2] Step 2 SUCCEEDED (USER_CONFIRMED + artifacts)")

    cf_ref = find_attached(plan, "step_2", ArtifactKind.CHANGED_FILES)
    evidence["changed_files"] = (
        store.read_text(cf_ref) if cf_ref is not None else "<missing>"
    )
    evidence["greeting_modified_from_fixture"] = (
        (ws / "greeting.py").read_text(encoding="utf-8") if (ws / "greeting.py").exists() else "<missing>"
    ) != FIXTURE_GREETING
    evidence["greeting_content"] = (
        (ws / "greeting.py").read_text(encoding="utf-8") if (ws / "greeting.py").exists() else "<missing>"
    )

    local_test = run_python_test(ws)
    evidence["local_test_after"] = local_test
    log(f"[9D6-H2] local test after Codex: passed={local_test['passed']} exit={local_test['exit']}")

    if plan.steps[2].state != WorkflowStepState.AWAITING_CONFIRMATION:
        issues.append(f"Step 3 did not reach AWAITING_CONFIRMATION (state={plan.steps[2].state.value}).")
        evidence["issues"] = issues
        write_evidence(evidence, evidence_arg, ws)
        log("[9D6-H2] RESULT=FAIL (Step 3 gate wrong after Step 2; no Review)")
        return 1

    # ---- Step 3 (optional): Review via the direct provider -----------------
    review_verdict = "skipped"
    if review:
        plan = coord.confirm_step(plan, "step_3")
        review_ex = ReviewStepExecutor(coord, store, parent=app)
        review_ex.execute(plan, "step_3")
        if not _wait_for_turn(review_ex, review_timeout_ms):
            issues.append("Step 3 timed out waiting for Direct Provider Review completion.")
            evidence["issues"] = issues
            write_evidence(evidence, evidence_arg, ws)
            log("[9D6-H2] RESULT=FAIL (Step 3 timeout)")
            return 1
        plan = review_ex.plan
        review_ref = find_attached(plan, "step_3", ArtifactKind.REVIEW)
        if review_ref is not None:
            review_text = store.read_text(review_ref)
            review_verdict = verdict_from_review(review_text)
            evidence["review_text"] = review_text
            evidence["review_verdict"] = review_verdict
        evidence["step3_state"] = plan.steps[2].state.value if plan else "?"
        if plan is None or plan.steps[2].state != WorkflowStepState.SUCCEEDED:
            issues.append("Step 3 did not SUCCEED.")
    else:
        evidence["step3_state"] = "skipped"

    evidence["final"] = {
        "step1_state": plan.steps[0].state.value,
        "step2_state": plan.steps[1].state.value,
        "step3_state": plan.steps[2].state.value,
        "workflow_state": plan.state.value,
    }
    evidence["issues"] = issues
    write_evidence(evidence, evidence_arg, ws)

    ok = not issues and plan.state == WorkflowState.SUCCEEDED
    log(f"[9D6-H2] workflow state={plan.state.value} review_verdict={review_verdict}")
    if ok:
        log("[9D6-H2] RESULT=PASS (workflow steps completed; see evidence JSON)")
        return 0
    log("[9D6-H2] RESULT=FAIL (workflow not SUCCEEDED)")
    return 1


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

def cleanup(workspace_arg: str | None) -> int:
    if not workspace_arg:
        errlog("--cleanup requires --workspace PATH of a disposable smoke workspace.")
        return 1
    ws = Path(workspace_arg).expanduser().resolve()
    if not str(ws).startswith(tempfile.gettempdir()):
        errlog(f"REFUSING to remove {ws}: not under the system temp directory.")
        return 1
    if not ws.is_dir():
        errlog(f"workspace {ws} does not exist.")
        return 1
    import shutil

    shutil.rmtree(ws)
    log(f"[9D6-H2] removed disposable workspace {ws}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Phase 9D.6-H2 staged resumable E2E smoke")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run-plan-and-implement", action="store_true")
    parser.add_argument("--continue", dest="continue_stage", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--state")
    parser.add_argument("--evidence")
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--plan-timeout-ms", type=int, default=PLAN_TIMEOUT_MS)
    parser.add_argument("--codex-timeout-ms", type=int, default=CODEX_TIMEOUT_MS)
    parser.add_argument("--review-timeout-ms", type=int, default=REVIEW_TIMEOUT_MS)
    args = parser.parse_args(argv)

    if args.prepare:
        return prepare(args.workspace)
    if args.run_plan_and_implement:
        if not args.workspace:
            errlog("--run-plan-and-implement requires --workspace PATH")
            return 2
        return run_plan_and_implement(
            args.workspace, args.state, args.evidence, args.plan_timeout_ms, args.codex_timeout_ms
        )
    if args.continue_stage:
        return continue_stage(args.state, args.evidence, args.review, args.review_timeout_ms)
    if args.cleanup:
        return cleanup(args.workspace)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
