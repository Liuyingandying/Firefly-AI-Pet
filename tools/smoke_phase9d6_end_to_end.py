"""Phase 9D.6 — Controlled real end-to-end smoke harness.

Composes the real production workflow classes (WorkflowCoordinator,
ArtifactStore, PlanStepExecutor, ImplementStepExecutor, ReviewStepExecutor) and
drives one real end-to-end pass:

    Claude Plan  ->  Codex Implement  ->  user confirm  ->  Claude Review

This is a validation harness, not a fake mini-workflow: every step transition
goes through the production executors and coordinator. No production source is
modified. The only files this tool writes besides the fixture are a control
marker directory and an evidence JSON (no secrets: no API keys, no env, no
session ids — only workflow id prefix, artifact paths, call counts, step
states, timings, and test results).

Human-in-the-loop gate: after the native Codex handoff the harness PAUSES and
waits for the user. It continues ONLY when a ``continue.txt`` file appears in
the control directory; it aborts on ``abort.txt``. It never auto-continues
from a sleep, a lifecycle hook, or a process exit.

Modes:
    --prepare [--workspace PATH]
        Create the disposable fixture (greeting.py / test_greeting.py /
        README.md) in a fresh disposable workspace and verify the initial
        ``python test_greeting.py`` FAILS. Prints the workspace path.

    --run --workspace PATH --control DIR [--evidence PATH]
        Run the full E2E against a prepared workspace. After the Codex
        handoff, waits for ``<control>/continue.txt`` or ``<control>/abort.txt``.
        Writes the evidence JSON (default: ``<workspace>/../phase9d6_evidence.json``
        is NOT used; defaults to a path printed on stderr).

    --cleanup [--workspace PATH]
        Remove a disposable smoke workspace after the report is done (only
        ever removes the disposable workspace, never production).
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
    WorkflowState,
    WorkflowStepState,
)
from ui.process_launcher import QuickAskRunner
from ui.workflow_executor import PlanStepExecutor
from ui.workflow_implement_executor import ImplementStepExecutor
from ui.workflow_review_executor import ReviewStepExecutor

SESSIONS_FILE = PROJECT_DIR / "config" / "sessions.json"
CLAUDE_SOURCE = PROJECT_DIR / "runtime" / "sources" / "claude.json"
CODEX_SOURCE = PROJECT_DIR / "runtime" / "sources" / "codex.json"
ARTIFACT_ROOT = PROJECT_DIR / "runtime" / "artifacts"
STEP_TIMEOUT_MS = 360_000  # 6 minutes per real Claude call

BUSINESS_FILES = ("greeting.py", "test_greeting.py", "README.md")

FIXTURE_GREETING = 'def greet(name):\n    return "Hello"\n'
FIXTURE_TEST = 'from greeting import greet\n\nassert greet("Firefly") == "Hello, Firefly!"\n'
FIXTURE_README = "Tiny disposable Phase 9D.6 test workspace.\n"


def log(msg: str) -> None:
    print(msg, flush=True)


def errlog(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# workspace helpers
# ---------------------------------------------------------------------------

def ws_snapshot(root: Path) -> dict[str, bytes]:
    """All files under root keyed by relative posix path (bytes)."""
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
    changed = []
    for name in BUSINESS_FILES:
        if before.get(name) != after.get(name):
            changed.append(name)
    return changed


def metadata_changes(before: dict[str, bytes], after: dict[str, bytes]) -> list[str]:
    changed = []
    for rel in set(before) | set(after):
        if rel.startswith((".claude/", ".codex/")):
            if before.get(rel) != after.get(rel):
                changed.append(rel)
    return changed


def run_python_test(workspace: Path) -> dict:
    """Run ``python test_greeting.py`` in the workspace; return evidence."""
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


def card_state(coordinator, plan, width=400) -> dict:
    """Best-effort WorkflowCard status snapshot (never blocks the E2E)."""
    try:
        from ui.workflow_card import WorkflowCard

        card = WorkflowCard(coordinator=coordinator, width=width)
        card._workflow_id = plan.workflow_id
        card._plan = plan
        card._refresh()
        rows = [r._status.text() for r in card._rows]
        overall = card._status.text()
        card.close()
        return {"overall": overall, "rows": rows}
    except Exception as exc:  # UI is optional; never fail the E2E on it
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def prepare(workspace_arg: str | None) -> int:
    if workspace_arg:
        ws = Path(workspace_arg).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
    else:
        ws = Path(tempfile.mkdtemp(prefix="firefly_phase9d6_"))
    if (ws / "greeting.py").exists():
        errlog(f"workspace {ws} already has greeting.py; refusing to overwrite fixture")
        return 1
    (ws / "greeting.py").write_text(FIXTURE_GREETING, encoding="utf-8")
    (ws / "test_greeting.py").write_text(FIXTURE_TEST, encoding="utf-8")
    (ws / "README.md").write_text(FIXTURE_README, encoding="utf-8")

    result = run_python_test(ws)
    log(f"[9D6] prepared workspace: {ws}")
    log(f"[9D6] initial test exit={result['exit']} passed={result['passed']}")
    if not result["ran"] or result["passed"] is not False:
        errlog("INITIAL FIXTURE INVALID: python test_greeting.py must FAIL on a fresh fixture.")
        return 1
    log("[9D6] initial test FAILs as required (greet() does not yet return 'Hello, Firefly!')")
    log(f"[9D6] initial test output tail:\n{result['output_tail']}")
    return 0


# ---------------------------------------------------------------------------
# run (full E2E)
# ---------------------------------------------------------------------------

def run_e2e(workspace_arg: str, control_arg: str, evidence_arg: str | None, gate_timeout: int) -> int:
    ws = Path(workspace_arg).expanduser().resolve()
    if not (ws / "greeting.py").is_file():
        errlog(f"workspace {ws} is not prepared (no greeting.py). Run --prepare first.")
        return 1

    initial = run_python_test(ws)
    if initial["passed"] is not False:
        errlog("INITIAL FIXTURE INVALID: python test_greeting.py must FAIL before Step 1.")
        return 1

    control = Path(control_arg).expanduser().resolve()
    control.mkdir(parents=True, exist_ok=True)
    continue_marker = control / "continue.txt"
    abort_marker = control / "abort.txt"
    for marker in (continue_marker, abort_marker):
        if marker.exists():
            marker.unlink()

    evidence: dict = {}
    issues: list[str] = []
    t_start = time.time()

    def record(**fields) -> None:
        evidence.update(fields)

    sessions_before = SESSIONS_FILE.read_bytes() if SESSIONS_FILE.exists() else None
    claude_before = CLAUDE_SOURCE.read_bytes() if CLAUDE_SOURCE.exists() else None
    codex_before = CODEX_SOURCE.read_bytes() if CODEX_SOURCE.exists() else None

    app = QApplication.instance() or QApplication([])
    store = ArtifactStore(ARTIFACT_ROOT)
    coord = WorkflowCoordinator()

    ws_before_plan = ws_snapshot(ws)
    req = TaskRequest(
        text="Fix greeting.py so the existing test passes. Do not modify test_greeting.py.",
        workspace=str(ws),
    )
    plan = coord.create_plan_implement_review(req)
    record(workflow_id_prefix=plan.workflow_id[:8], workspace=str(ws))

    agent_counts = {"claude_plan": 0, "codex_implement": 0, "claude_review": 0}

    # ---- Step 1: real Claude Plan ---------------------------------------
    log(f"[9D6] workflow={plan.workflow_id} created; confirming Step 1")
    plan = coord.confirm_step(plan, "step_1")

    runner1 = QuickAskRunner(session_manager=None, parent=app)
    plan_ex = PlanStepExecutor(coord, store, runner=runner1, parent=app)
    loop_done: list[bool] = []
    plan_ex.turn_finished.connect(lambda: loop_done.append(True))
    loop = QEventLoop()
    plan_ex.turn_finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)

    try:
        plan_ex.execute(plan, "step_1")
    except Exception as exc:
        issues.append(f"Step 1 execute raised: {type(exc).__name__}: {exc}")
        record(final={"step1_state": "error", "step2_state": "error", "step3_state": "error",
                      "workflow_state": "error"},
               issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 1 pre-launch error; Codex not started)")
        return 1

    if plan_ex.running:
        agent_counts["claude_plan"] = 1
        log("[9D6] Step 1 running (real Claude Plan call #1)")
    timer.start(STEP_TIMEOUT_MS)
    loop.exec()

    plan = plan_ex.plan
    if not loop_done:
        issues.append("Step 1 timed out waiting for the managed Claude call.")
        record(final=step_states(plan), issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 1 timeout; Codex not started)")
        return 1

    record(step1={
        "state_after": plan.steps[0].state.value,
        "plan_artifact": artifact_info(plan, store, ArtifactKind.PLAN, "step_1"),
    })
    log(f"[9D6] Step 1 state={plan.steps[0].state.value}")

    ws_after_plan = ws_snapshot(ws)
    record(plan_phase_workspace={
        "business_changed": business_changes(ws_before_plan, ws_after_plan),
        "tool_metadata_changed": metadata_changes(ws_before_plan, ws_after_plan),
    })

    if plan.steps[0].state != WorkflowStepState.SUCCEEDED:
        issues.append(f"Step 1 did not SUCCEED (state={plan.steps[0].state.value}).")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 1 failed; Codex not started)")
        return 1
    if plan.steps[1].state != WorkflowStepState.AWAITING_CONFIRMATION:
        issues.append(f"Step 2 did not reach AWAITING_CONFIRMATION (state={plan.steps[1].state.value}).")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 2 gate wrong after Step 1)")
        return 1

    log(f"[9D6] Step 2 AWAITING_CONFIRMATION (not auto-executed)")
    record(card_after_plan=card_state(coord, plan))

    # ---- Step 2: user confirms, real Codex ------------------------------
    plan = coord.confirm_step(plan, "step_2")
    impl_ex = ImplementStepExecutor(coord, store, parent=app)
    try:
        plan = impl_ex.execute(plan, "step_2")
    except Exception as exc:
        issues.append(f"Step 2 execute raised: {type(exc).__name__}: {exc}")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 2 pre-launch error; Codex not started)")
        return 1

    step2_after_execute = plan.steps[1].state.value
    if step2_after_execute == WorkflowStepState.RUNNING:
        agent_counts["codex_implement"] = 1
        log("[9D6] Step 2 RUNNING: native Codex handed the disposable task (call #1)")
    else:
        issues.append(f"Step 2 not RUNNING after execute (state={step2_after_execute}).")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Codex launch failed)")
        return 1
    record(step2={"handoff_state": "RUNNING", "handoff_message": "Codex handed the disposable task"},
           card_after_step2_execute=card_state(coord, plan))

    # ---- HUMAN GATE: wait for the user to confirm Codex finished ---------
    log("")
    log("Codex has been handed the disposable task.")
    log("Please wait for Codex to finish.")
    log("When it is finished, return here and confirm continuation.")
    log("")
    log(f"[9D6] GATE_WAITING control_dir={control}")
    t_gate = time.time()
    gate_decision = None
    while True:
        if continue_marker.exists():
            gate_decision = "continue"
            break
        if abort_marker.exists():
            gate_decision = "abort"
            break
        if gate_timeout > 0 and (time.time() - t_gate) > gate_timeout:
            gate_decision = "timeout"
            break
        if int(time.time() - t_gate) % 60 == 0 and int(time.time() - t_gate) > 0:
            log(f"[9D6] gate heartbeat: still waiting for user confirmation ({int(time.time()-t_gate)}s)")
        time.sleep(2)

    gate_wait_s = round(time.time() - t_gate, 1)
    record(gate={"decision": gate_decision, "wait_seconds": gate_wait_s})
    log(f"[9D6] GATE decision={gate_decision} after {gate_wait_s}s")
    if gate_decision != "continue":
        issues.append(f"User gate resolved as {gate_decision}; Step 2 completion not confirmed.")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=ABORTED (no Review; workflow stays RUNNING)")
        return 2

    # ---- Step 2 completion (user-confirmed, production path) ------------
    attempt = impl_ex.confirm_completion(plan, "step_2")
    record(step2_completion={
        "ok": attempt.ok,
        "no_changes": attempt.no_changes,
        "snapshot_error": attempt.snapshot_error,
        "message": attempt.message,
    })
    plan = impl_ex.plan or plan
    if not attempt.ok:
        issues.append(f"Step 2 completion not accepted: {attempt.message}")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 2 completion rejected; no Review)")
        return 1

    log(f"[9D6] Step 2 SUCCEEDED (USER_CONFIRMED + artifacts)")

    # Workspace evidence after Codex (business files only + metadata).
    ws_after_implement = ws_snapshot(ws)
    step2_business = business_changes(ws_before_plan, ws_after_implement)
    record(step2_workspace={
        "business_changed": step2_business,
        "greeting_modified": "greeting.py" in step2_business,
        "test_greeting_modified": "test_greeting.py" in step2_business,
        "readme_modified": "README.md" in step2_business,
        "tool_metadata_changed": metadata_changes(ws_before_plan, ws_after_implement),
    })
    record(greeting_content=(ws / "greeting.py").read_text(encoding="utf-8") if (ws / "greeting.py").exists() else "<missing>")

    local_test = run_python_test(ws)
    record(local_test_after=local_test)
    log(f"[9D6] local test after Codex: passed={local_test['passed']} exit={local_test['exit']}")

    cf_ref = find_attached(plan, "step_2", ArtifactKind.CHANGED_FILES)
    summary_ref = find_attached(plan, "step_2", ArtifactKind.IMPLEMENTATION_SUMMARY)
    record(step2_artifacts={
        "changed_files": artifact_info(plan, store, ArtifactKind.CHANGED_FILES, "step_2"),
        "implementation_summary": artifact_info(plan, store, ArtifactKind.IMPLEMENTATION_SUMMARY, "step_2"),
    })
    if cf_ref is not None:
        cf_text = store.read_text(cf_ref)
        record(changed_files_text=cf_text)
        record(changed_files_contains={
            "greeting.py": bool(re.search(r"greeting\.py", cf_text)),
            "test_greeting.py": bool(re.search(r"test_greeting\.py", cf_text)),
        })
    if summary_ref is not None:
        record(implementation_summary_text=store.read_text(summary_ref))

    if plan.steps[2].state != WorkflowStepState.AWAITING_CONFIRMATION:
        issues.append(f"Step 3 did not reach AWAITING_CONFIRMATION (state={plan.steps[2].state.value}).")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 3 gate wrong after Step 2; no Review)")
        return 1
    record(card_after_step2_complete=card_state(coord, plan))

    # ---- Step 3: user confirms, real Claude Review ----------------------
    log("[9D6] Step 3 AWAITING_CONFIRMATION; confirming Step 3")
    plan = coord.confirm_step(plan, "step_3")

    ws_before_review = ws_snapshot(ws)
    runner3 = QuickAskRunner(session_manager=None, parent=app)
    review_ex = ReviewStepExecutor(coord, store, runner=runner3, parent=app)
    loop_done3: list[bool] = []
    review_ex.turn_finished.connect(lambda: loop_done3.append(True))
    loop3 = QEventLoop()
    review_ex.turn_finished.connect(loop3.quit)
    timer3 = QTimer()
    timer3.setSingleShot(True)
    timer3.timeout.connect(loop3.quit)

    try:
        review_ex.execute(plan, "step_3")
    except Exception as exc:
        issues.append(f"Step 3 execute raised: {type(exc).__name__}: {exc}")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 3 pre-launch error)")
        return 1
    if review_ex.running:
        agent_counts["claude_review"] = 1
        log("[9D6] Step 3 running (real Claude Review call #1)")
    timer3.start(STEP_TIMEOUT_MS)
    loop3.exec()

    plan = review_ex.plan
    if not loop_done3:
        issues.append("Step 3 timed out waiting for the managed Claude call.")
        record(agent_counts=agent_counts, issues=issues)
        write_evidence(evidence, evidence_arg, ws, control)
        log("[9D6] RESULT=FAIL (Step 3 timeout)")
        return 1

    record(step3={
        "state_after": plan.steps[2].state.value,
        "review_artifact": artifact_info(plan, store, ArtifactKind.REVIEW, "step_3"),
    })
    ws_after_review = ws_snapshot(ws)
    record(review_phase_workspace={
        "business_changed": business_changes(ws_before_review, ws_after_review),
        "tool_metadata_changed": metadata_changes(ws_before_review, ws_after_review),
    })

    review_ref = find_attached(plan, "step_3", ArtifactKind.REVIEW)
    if review_ref is not None:
        review_text = store.read_text(review_ref)
        record(review_text=review_text)
        record(review_verdict=verdict_from_review(review_text))

    if plan.steps[2].state != WorkflowStepState.SUCCEEDED:
        issues.append(f"Step 3 did not SUCCEED (state={plan.steps[2].state.value}).")
    record(card_final=card_state(coord, plan))

    # ---- final states + isolation ---------------------------------------
    record(final=step_states(plan))
    record(agent_counts=agent_counts)
    record(duration_seconds=round(time.time() - t_start, 1))

    sessions_after = SESSIONS_FILE.read_bytes() if SESSIONS_FILE.exists() else None
    claude_after = CLAUDE_SOURCE.read_bytes() if CLAUDE_SOURCE.exists() else None
    codex_after = CODEX_SOURCE.read_bytes() if CODEX_SOURCE.exists() else None
    record(isolation={
        "sessions_json_unchanged": sessions_after == sessions_before,
        "claude_lifecycle_unchanged": claude_after == claude_before,
        "codex_lifecycle_before": codex_before.decode("utf-8", "replace") if codex_before else None,
        "codex_lifecycle_after": codex_after.decode("utf-8", "replace") if codex_after else None,
        "codex_lifecycle_observed": codex_after != codex_before,
    })

    record(issues=issues)
    write_evidence(evidence, evidence_arg, ws, control)

    log(f"[9D6] workflow state={plan.state.value}")
    log(f"[9D6] step states: {plan.steps[0].state.value} / {plan.steps[1].state.value} / {plan.steps[2].state.value}")
    if plan.state == WorkflowState.SUCCEEDED:
        log("[9D6] RESULT=PASS (workflow steps completed; see evidence JSON)")
        return 0
    log("[9D6] RESULT=FAIL (workflow not SUCCEEDED)")
    return 1


def step_states(plan) -> dict:
    return {
        "step1_state": plan.steps[0].state.value,
        "step2_state": plan.steps[1].state.value,
        "step3_state": plan.steps[2].state.value,
        "workflow_state": plan.state.value,
    }


def find_attached(plan, step_id: str, kind: ArtifactKind):
    for step in plan.steps:
        if step.step_id == step_id:
            for ref in step.attached_artifacts:
                if ref.kind == kind:
                    return ref
    return None


def artifact_info(plan, store: ArtifactStore, kind: ArtifactKind, step_id: str) -> dict | None:
    ref = find_attached(plan, step_id, kind)
    if ref is None:
        return None
    info = {
        "path": ref.path,
        "size": ref.metadata.get("size") if ref.metadata else None,
        "sha256_prefix": (ref.metadata.get("sha256") or "")[:12] if ref.metadata else None,
    }
    try:
        info["excerpt"] = store.read_text(ref)[:800]
    except Exception as exc:
        info["excerpt"] = f"<read error: {exc}>"
    return info


def write_evidence(evidence: dict, evidence_arg: str | None, ws: Path, control: Path) -> None:
    path = Path(evidence_arg) if evidence_arg else (ws.parent / "phase9d6_evidence.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[9D6] evidence written: {path}")


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
    log(f"[9D6] removed disposable workspace {ws}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Phase 9D.6 controlled E2E smoke")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--control")
    parser.add_argument("--evidence")
    parser.add_argument("--gate-timeout-seconds", type=int, default=0)
    args = parser.parse_args(argv)

    if args.prepare:
        return prepare(args.workspace)
    if args.run:
        if not args.control:
            errlog("--run requires --control DIR (human gate marker directory)")
            return 2
        return run_e2e(args.workspace, args.control, args.evidence, args.gate_timeout_seconds)
    if args.cleanup:
        return cleanup(args.workspace)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
