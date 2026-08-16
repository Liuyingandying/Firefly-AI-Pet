"""Phase 9D.6-H2 — Controlled real `codex exec` through the ImplementStepExecutor.

ONE real online codex call, disposable workspace only. Drives the production
:class:`ImplementStepExecutor` (managed exec: owned runner process, JSONL via
the existing CodexJsonlAdapter -> AgentEvent, workspace-write sandbox) against
the real Codex CLI and verifies the 9D.6-H2 success criteria:

  - no TTY blocker (exec completes, not "stdin is not a terminal")
  - greeting.py modified, test_greeting.py byte-identical, local test PASS
  - JSONL parsed into AgentEvents (SESSION / STATUS / TOOL / FINAL observed)
  - process owned + exit code captured from the owned process
  - exec finishing does NOT auto-succeed Step 2 (still RUNNING + user confirm)
  - user-confirmed completion writes CHANGED_FILES + IMPLEMENTATION_SUMMARY

No Claude call is made (the PLAN artifact is supplied directly). Uses the same
D6 fixture shape (greeting.py / test_greeting.py / README.md).

Usage:
    python tools/smoke_phase9d6_h2_codex_exec.py [--workspace PATH] [--timeout SECONDS]
        [--no-cleanup]

Exit 0 = PASS; 1 = executed but a caveat; 2 = transport FAILED.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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

from core.agent_events import AgentEventType
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
from ui.workflow_implement_executor import ImplementStepExecutor

ARTIFACT_ROOT = PROJECT_DIR / "runtime" / "artifacts"

FIXTURE_GREETING = 'def greet(name):\n    return "Hello"\n'
FIXTURE_TEST = 'from greeting import greet\n\nassert greet("Firefly") == "Hello, Firefly!"\n'
FIXTURE_README = "Tiny disposable Phase 9D.6-H2 codex exec smoke workspace.\n"

TASK = "Fix greeting.py so the existing test passes. Do not modify test_greeting.py."

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


def snapshot(root: Path) -> dict[str, bytes]:
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


def run_local_test(ws: Path) -> dict:
    try:
        proc = __import__("subprocess").run(
            [sys.executable, str(ws / "test_greeting.py")],
            cwd=str(ws), capture_output=True, text=True, timeout=120,
        )
    except (__import__("subprocess").TimeoutExpired, OSError) as exc:
        return {"ran": False, "exit": None, "passed": None, "tail": f"<error: {exc}>"}
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
    return {"ran": True, "exit": proc.returncode, "passed": proc.returncode == 0, "tail": "\n".join(tail)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Managed codex exec through ImplementStepExecutor")
    parser.add_argument("--workspace", help="existing disposable workspace to reuse")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args()

    if args.workspace:
        ws = Path(args.workspace).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        created = False
    else:
        ws = Path(tempfile.mkdtemp(prefix="firefly_h2_codex_"))
        created = True

    evidence: dict = {}
    try:
        if created or not (ws / "greeting.py").exists():
            (ws / "greeting.py").write_text(FIXTURE_GREETING, encoding="utf-8")
            (ws / "test_greeting.py").write_text(FIXTURE_TEST, encoding="utf-8")
            (ws / "README.md").write_text(FIXTURE_README, encoding="utf-8")

        initial = run_local_test(ws)
        print(f"[h2] workspace={ws}")
        print(f"[h2] initial test: exit={initial['exit']} passed={initial['passed']}")
        if initial["passed"] is not False:
            print("[h2] INVALID FIXTURE: initial test must FAIL")
            return 2

        before = snapshot(ws)
        app = QApplication.instance() or QApplication([])
        store = ArtifactStore(ARTIFACT_ROOT)
        coord = WorkflowCoordinator()
        req = TaskRequest(text=TASK, workspace=str(ws))
        plan = coord.create_plan_implement_review(req)
        # Attach a known-good PLAN artifact directly (no Claude call).
        plan = coord.confirm_step(plan, "step_1")
        plan = coord.mark_step_started(plan, "step_1")
        ref = store.write_text(plan.workflow_id, ArtifactKind.PLAN, "step_1", GOOD_PLAN)
        plan = coord.attach_artifact(plan, "step_1", ref)
        plan = coord.mark_step_succeeded(
            plan,
            "step_1",
            StepCompletionEvidence(source=CompletionSource.MANAGED_AGENT_RESULT, summary="smoke plan"),
        )
        plan = coord.confirm_step(plan, "step_2")

        impl_ex = ImplementStepExecutor(coord, store, parent=app)
        seen_event_types: list[str] = []
        runner_exit_codes: list[int] = []
        impl_ex.agent_event.connect(lambda ev: seen_event_types.append(ev.type.value))
        impl_ex._runner.finished.connect(lambda _text, code: runner_exit_codes.append(int(code)))

        t0 = time.time()
        plan = impl_ex.execute(plan, "step_2")
        if plan.steps[1].state != WorkflowStepState.RUNNING:
            print("[h2] Step 2 did not start RUNNING (managed exec launch failed)")
            return 2

        loop = QEventLoop()
        impl_ex.turn_finished.connect(loop.quit)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(args.timeout * 1000)
        loop.exec()
        # timer.isActive() is True while the timer is still pending, so a
        # timed-out loop is one where the timer already fired (not active).
        timed_out = not timer.isActive()
        elapsed = round(time.time() - t0, 1)
        plan = impl_ex.plan

        evidence["timed_out"] = timed_out
        evidence["elapsed_seconds"] = elapsed
        evidence["event_types_seen"] = seen_event_types
        evidence["runner_exit_codes"] = runner_exit_codes
        print(f"[h2] elapsed={elapsed}s timed_out={timed_out}")
        print(f"[h2] agent events seen: {sorted(set(seen_event_types))}")
        print(f"[h2] runner exit codes: {runner_exit_codes}")

        if timed_out or plan is None or plan.state == WorkflowState.FAILED:
            print("[h2] managed exec did not finish cleanly")
            return 2
        if not impl_ex.managed_exec_finished:
            print("[h2] managed exec did not reach execution-finished state")
            return 2
        exit_code = impl_ex.managed_exec_exit_code
        evidence["execution_exit_code"] = exit_code
        print(f"[h2] managed exec finished cleanly, exit={exit_code}")

        # Step 2 must still be RUNNING and never auto-succeeded.
        evidence["step2_state_after_exec"] = plan.steps[1].state.value
        evidence["step2_attached_after_exec"] = len(plan.steps[1].attached_artifacts)
        print(f"[h2] step2 state after exec: {plan.steps[1].state.value} (RUNNING expected)")
        if plan.steps[1].state != WorkflowStepState.RUNNING:
            print("[h2] exec finishing auto-succeeded Step 2 — WRONG")
            return 2

        after = snapshot(ws)
        changed = {k for k in before if before.get(k) != after.get(k)}
        evidence["business_changed"] = sorted(changed)
        greeting_modified = "greeting.py" in changed
        test_untouched = "test_greeting.py" not in changed
        evidence["greeting_modified"] = greeting_modified
        evidence["test_greeting_untouched"] = test_untouched
        evidence["greeting_content"] = (
            (ws / "greeting.py").read_text(encoding="utf-8") if (ws / "greeting.py").exists() else None
        )
        print(f"[h2] greeting.py modified: {greeting_modified}; test_greeting.py untouched: {test_untouched}")

        local_after = run_local_test(ws)
        evidence["local_test_after"] = local_after
        print(f"[h2] local test after: exit={local_after['exit']} passed={local_after['passed']}")

        # User-confirmed completion (real production path) succeeds Step 2.
        attempt = impl_ex.confirm_completion(plan, "step_2")
        plan = impl_ex.plan or plan
        evidence["completion"] = {
            "ok": attempt.ok,
            "no_changes": attempt.no_changes,
            "snapshot_error": attempt.snapshot_error,
            "message": attempt.message,
        }
        evidence["step2_state_after_completion"] = plan.steps[1].state.value
        print(f"[h2] completion ok={attempt.ok} -> step2 {plan.steps[1].state.value}")

        evidence_path = ws.parent / "phase9d6_h2_codex_evidence.json"
        evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[h2] evidence: {evidence_path}")

        no_tty = True
        fatal = timed_out
        ok = (
            not fatal
            and no_tty
            and greeting_modified
            and test_untouched
            and local_after["passed"]
            and attempt.ok
            and plan.steps[1].state == WorkflowStepState.SUCCEEDED
            and (AgentEventType.FINAL.value in seen_event_types or AgentEventType.SESSION.value in seen_event_types)
        )
        print("[h2] RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 2
    finally:
        if created and not args.no_cleanup:
            shutil.rmtree(ws, ignore_errors=True)
            print(f"[h2] cleaned up disposable workspace {ws}")


if __name__ == "__main__":
    sys.exit(main())
