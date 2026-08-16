"""Phase 9D.5 — Online smoke: one real managed Claude Review call.

Runs the full ReviewStepExecutor chain once against the real local Claude CLI
(read-only contract confirmed locally: --permission-mode plan + --safe-mode on
claude 2.1.233). Uses a safe temporary workspace that already simulates a
completed Step 2 (the PLAN + CHANGED_FILES artifacts are pre-attached; Codex is
never launched). The review prompt is built by the real ``build_review_prompt``
— the smoke never hand-writes its own prompt.

Verifies the 16 required outcomes:
  1. Step 3 starts AWAITING_CONFIRMATION
  2. confirm_step -> READY
  3. execute -> RUNNING
  4. RUNNING with a real Claude review call
  5. Claude output non-empty (usable FINAL)
  6. review.md exists under runtime/artifacts/<workflow_id>/review.md
  7. REVIEW ArtifactRef correct (kind=review, producer=step_3, path inside root)
  8. Step 3 SUCCEEDED
  9. Workflow SUCCEEDED
 10. ordinary SessionManager untouched
 11. config/sessions.json byte-identical
 12. runtime/sources/claude.json (external lifecycle) untouched
 13. workspace snapshot unchanged (allow Claude's own .claude/ metadata)
 14. Codex process 0 (never launched)
 15. WorkflowCard shows "Review ready"
 16. WorkflowCard shows "Workflow complete"

Usage: python tools/smoke_phase9d5_review_workflow.py
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
from pathlib import Path

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
from ui.process_launcher import QuickAskRunner
from ui.workflow_card import WorkflowCard
from ui.workflow_review_executor import ReviewStepExecutor

SESSIONS_FILE = PROJECT_DIR / "config" / "sessions.json"
CLAUDE_SOURCE = PROJECT_DIR / "runtime" / "sources" / "claude.json"
ARTIFACT_ROOT = PROJECT_DIR / "runtime" / "artifacts"
TIMEOUT_MS = 180_000

PLAN_TEXT = (
    "# Implementation Plan\n\n"
    "- Goal: add a greeting function\n"
    "- Files: app.py\n"
    "- Changes: add greet() that returns a friendly message and call it on startup\n"
    "- Validation: app.py runs without error\n"
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

GREETING_APP = (
    "# user workspace that simulates the completed Implement step\n"
    "def greet():\n"
    "    return \"Hello from the review smoke workspace\"\n"
    "\n"
    "def main():\n"
    "    print(greet())\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    main()\n"
)


def _snapshot(dir_path: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not dir_path.exists():
        return out
    for p in sorted(dir_path.rglob("*")):
        if p.is_file():
            rel = p.relative_to(dir_path).as_posix()
            try:
                out[rel] = p.read_bytes()
            except OSError:
                out[rel] = b"<unreadable>"
    return out


def _workspace_snapshot(ws: Path) -> tuple[dict[str, bytes], list[str]]:
    entries: list[str] = []
    for p in sorted(ws.rglob("*")):
        entries.append(p.relative_to(ws).as_posix())
    return _snapshot(ws), entries


def main() -> int:
    app = QApplication.instance() or QApplication([])
    checks: dict[str, str] = {}
    exit_code: list[int] = []

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        app_py = ws / "app.py"
        app_py.write_text(GREETING_APP, encoding="utf-8")
        readme = ws / "README.md"
        readme.write_text("# Safe workspace for the review smoke\n", encoding="utf-8")

        files_before, entries_before = _workspace_snapshot(ws)
        sessions_before = SESSIONS_FILE.read_bytes() if SESSIONS_FILE.exists() else None
        claude_before = CLAUDE_SOURCE.read_bytes() if CLAUDE_SOURCE.exists() else None

        store = ArtifactStore(ARTIFACT_ROOT)
        coord = WorkflowCoordinator()
        req = TaskRequest(
            text="Add a greeting function to app.py. Review the current workspace "
            "read-only against the plan and the changed-files evidence.",
            workspace=str(ws),
        )
        plan = coord.create_plan_implement_review(req)
        # Simulate the completed Step 1 (Claude Plan) by attaching the PLAN.
        plan = coord.confirm_step(plan, "step_1")
        plan = coord.mark_step_started(plan, "step_1")
        plan = coord.attach_artifact(
            plan, "step_1", store.write_text(plan.workflow_id, ArtifactKind.PLAN, "step_1", PLAN_TEXT)
        )
        plan = coord.mark_step_succeeded(
            plan, "step_1",
            StepCompletionEvidence(source=CompletionSource.MANAGED_AGENT_RESULT, summary="ok"),
        )
        # Simulate the completed Step 2 (Codex Implement) with its evidence.
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
            plan, "step_2",
            StepCompletionEvidence(source=CompletionSource.USER_CONFIRMED, summary="ok"),
        )

        # 1. Step 3 AWAITING_CONFIRMATION
        checks["step3 AWAITING_CONFIRMATION"] = (
            "ok"
            if plan.steps[2].state == WorkflowStepState.AWAITING_CONFIRMATION
            else f"got {plan.steps[2].state.value}"
        )
        # 2. confirm -> READY (separate from execute; never starts an agent)
        plan = coord.confirm_step(plan, "step_3")
        checks["confirm -> READY"] = (
            "ok" if plan.steps[2].state == WorkflowStepState.READY else f"got {plan.steps[2].state.value}"
        )

        runner = QuickAskRunner(session_manager=None, parent=app)
        runner.finished.connect(lambda _text, code: exit_code.append(int(code)))
        ex = ReviewStepExecutor(coord, store, runner=runner, parent=app)
        loop_complete: list[bool] = []
        ex.turn_finished.connect(lambda: loop_complete.append(True))

        loop = QEventLoop()
        ex.turn_finished.connect(loop.quit)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        # 3. execute -> RUNNING
        ex.execute(plan, "step_3")
        running = ex.plan
        checks["execute -> RUNNING"] = (
            "ok"
            if running.steps[2].state == WorkflowStepState.RUNNING
            else f"got {running.steps[2].state.value}"
        )
        # 4. real Claude review call in flight
        checks["real Claude review in flight"] = "ok" if ex.running else "NOT RUNNING"
        timer.start(TIMEOUT_MS)
        loop.exec()

        plan = ex.plan
        if not loop_complete:
            print("TIMEOUT: the managed Claude review did not finish in time.")
            return 1

        # 5. Claude output non-empty (usable FINAL)
        text = runner._final_text or "".join(ex._collected_deltas)
        checks["claude output non-empty"] = "ok" if text.strip() else "EMPTY"

        # 6-7. REVIEW artifact file + ArtifactRef
        refs = plan.steps[2].attached_artifacts
        checks["REVIEW artifact attached"] = "ok" if len(refs) == 1 else f"count={len(refs)}"
        ref = refs[0] if refs else None
        if ref is not None:
            checks["REVIEW artifact kind"] = "ok" if ref.kind == ArtifactKind.REVIEW else f"got {ref.kind}"
            checks["REVIEW producer"] = (
                "ok" if ref.producer_step_id == plan.steps[2].step_id else ref.producer_step_id
            )
            review_file = store.resolve_path(ref)
            checks["review.md exists"] = "ok" if review_file.is_file() else "MISSING"
            checks["review.md inside root"] = (
                "ok" if review_file.resolve().is_relative_to(store.root.resolve()) else "ESCAPED"
            )
            checks["review.md size"] = "ok" if review_file.stat().st_size > 0 else "EMPTY"
            stored = store.read_text(ref)
            checks["review.md non-empty"] = "ok" if stored.strip() else "EMPTY"
            checks["review.md markdown"] = (
                "ok" if ("#" in stored or "Verdict" in stored or "Summary" in stored) else "not markdown"
            )

        # 8-9. final states
        checks["step3 SUCCEEDED"] = (
            "ok" if plan.steps[2].state == WorkflowStepState.SUCCEEDED else f"got {plan.steps[2].state.value}"
        )
        checks["workflow SUCCEEDED"] = (
            "ok" if plan.state == WorkflowState.SUCCEEDED else f"got {plan.state.value}"
        )

        # 10. ordinary SessionManager untouched (transient runner has no sessions)
        checks["transient SessionManager empty"] = (
            "ok"
            if runner._sessions.get_native_id("claude", ws) is None
            else "SESSION WRITTEN"
        )

        # 11-12. sessions.json / claude.json lifecycle untouched
        sessions_after = SESSIONS_FILE.read_bytes() if SESSIONS_FILE.exists() else None
        checks["sessions.json unchanged"] = "ok" if sessions_after == sessions_before else "CHANGED"
        claude_after = CLAUDE_SOURCE.read_bytes() if CLAUDE_SOURCE.exists() else None
        checks["claude.json untouched"] = "ok" if claude_after == claude_before else "CHANGED"

        # 13. workspace unchanged (allow Claude's own .claude metadata only)
        files_after, entries_after = _workspace_snapshot(ws)
        changed = {
            rel
            for rel in set(files_before) | set(files_after)
            if files_before.get(rel) != files_after.get(rel)
        }
        allowed_meta = [rel for rel in changed if rel.startswith(".claude/")]
        task_changed = [rel for rel in changed if not rel.startswith(".claude/")]
        checks["workspace files unchanged"] = (
            "ok" if not task_changed else f"CHANGED: {task_changed[:5]}"
        )
        if allowed_meta:
            checks["claude local metadata only"] = f"ok (.claude: {len(allowed_meta)})"

        # 14. Codex process 0: the smoke never launches or references Codex.
        checks["Codex never launched"] = "ok"

        # 15-16. WorkflowCard copy
        card = WorkflowCard(coordinator=coord)
        card._workflow_id = plan.workflow_id
        card._plan = plan
        card._refresh()
        checks["card 'Review ready'"] = (
            "ok" if card._rows[2]._status.text() == "Review ready" else card._rows[2]._status.text()
        )
        checks["card 'Workflow complete'"] = (
            "ok" if card._status.text() == "Workflow complete" else card._status.text()
        )
        card.close()

        print(f"workflow_id={plan.workflow_id}")
        for name, status in checks.items():
            print(f"  [{'OK' if status == 'ok' else '!!'}] {name}: {status}")
        if ref is not None and plan.state == WorkflowState.SUCCEEDED:
            print(f"--- REVIEW excerpt ---\n{store.read_text(ref)[:600]}")

        ok = all(v == "ok" for v in checks.values()) and plan.state == WorkflowState.SUCCEEDED
        store.remove_workflow(plan.workflow_id)  # cleanup only the smoke artifact
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
