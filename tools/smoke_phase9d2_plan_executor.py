"""Phase 9D.2 — Online smoke: one real managed Claude Plan call.

Runs the full PlanStepExecutor chain once against the real local Claude CLI
(read-only contract confirmed locally: --permission-mode plan + --safe-mode on
claude 2.1.233). Uses a safe temporary workspace, never the real project.

Verifies the 10 required outcomes:
  1. Claude output non-empty
  2. PLAN file exists under runtime/artifacts/<workflow_id>/plan.md
  3. ArtifactRef correct (kind=plan, producer=step_1, path inside root)
  4. Step 1 SUCCEEDED
  5. Step 2 AWAITING_CONFIRMATION
  6. Step 2 NOT started (no Codex)
  7. config/sessions.json byte-identical
  8. runtime/sources/claude.json (external lifecycle) untouched
  9. temp workspace files unchanged (read-only contract held)
 10. process exit clean (exit code 0)

Usage: python tools/smoke_phase9d2_plan_executor.py
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.artifact_store import ArtifactStore
from core.routing_models import TaskRequest
from core.session_store import SessionStore
from core.workflow_coordinator import WorkflowCoordinator
from core.workflow_models import ArtifactKind, WorkflowEventType, WorkflowState, WorkflowStepState
from ui.process_launcher import QuickAskRunner
from ui.workflow_executor import PlanStepExecutor

SESSIONS_FILE = PROJECT_DIR / "config" / "sessions.json"
CLAUDE_SOURCE = PROJECT_DIR / "runtime" / "sources" / "claude.json"
ARTIFACT_ROOT = PROJECT_DIR / "runtime" / "artifacts"
TIMEOUT_MS = 180_000


def _snapshot(dir_path: Path) -> dict[str, bytes]:
    """All files under dir_path, keyed by relative posix path, bytes."""
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
    """Files plus a list of all relative entries (dirs and files)."""
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
        marker = ws / "hello.py"
        marker.write_text("# user file that must not change\ndef hi():\n    return 42\n", encoding="utf-8")
        readme = ws / "README.md"
        readme.write_text("# Safe workspace for the plan smoke\n", encoding="utf-8")

        files_before, entries_before = _workspace_snapshot(ws)
        sessions_before = SESSIONS_FILE.read_bytes() if SESSIONS_FILE.exists() else None
        claude_before = CLAUDE_SOURCE.read_bytes() if CLAUDE_SOURCE.exists() else None

        store = ArtifactStore(ARTIFACT_ROOT)
        coord = WorkflowCoordinator()
        failed_codes: list[str] = []
        coord.connect(lambda e: failed_codes.append(e.error_code) if e.type == WorkflowEventType.STEP_FAILED else None)
        req = TaskRequest(
            text="Create an implementation plan for adding a greeting function. Do not modify files.",
            workspace=str(ws),
        )
        plan = coord.create_plan_implement_review(req)
        plan = coord.confirm_step(plan, plan.steps[0].step_id)

        runner = QuickAskRunner(session_manager=None, parent=app)
        runner.finished.connect(lambda _text, code: exit_code.append(int(code)))
        ex = PlanStepExecutor(coord, store, runner=runner, parent=app)

        loop_complete: list[bool] = []
        ex.turn_finished.connect(lambda: loop_complete.append(True))

        from PySide6.QtCore import QEventLoop

        loop = QEventLoop()
        ex.turn_finished.connect(loop.quit)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        ex.execute(plan, plan.steps[0].step_id)
        running = ex.plan
        checks["READY->RUNNING"] = (
            "ok" if running.steps[0].state == WorkflowStepState.RUNNING else f"got {running.steps[0].state.value}"
        )
        timer.start(TIMEOUT_MS)
        loop.exec()

        plan = ex.plan
        if not loop_complete:
            print("TIMEOUT: the managed Claude call did not finish in time.")
            return 1

        # 1. output non-empty
        text = runner._final_text or "".join(ex._collected_deltas)
        checks["claude output non-empty"] = "ok" if text.strip() else "EMPTY"
        if text.strip() and plan.steps[0].state != WorkflowStepState.SUCCEEDED:
            print("--- rejected plan text (first 1000 chars) ---")
            print(text[:1000])
            print("--- failure error codes ---")
            print(failed_codes or ["(none)"])

        # 2-3. PLAN artifact file + ArtifactRef
        refs = plan.steps[0].attached_artifacts
        checks["PLAN artifact attached"] = "ok" if len(refs) == 1 else f"count={len(refs)}"
        ref = refs[0] if refs else None
        if ref is not None:
            checks["PLAN artifact kind"] = "ok" if ref.kind == ArtifactKind.PLAN else f"got {ref.kind}"
            checks["PLAN producer"] = "ok" if ref.producer_step_id == plan.steps[0].step_id else ref.producer_step_id
            plan_file = store.resolve_path(ref)
            checks["PLAN file exists"] = "ok" if plan_file.is_file() else "MISSING"
            checks["PLAN file inside root"] = "ok" if plan_file.resolve().is_relative_to(store.root.resolve()) else "ESCAPED"
            checks["PLAN file size"] = "ok" if plan_file.stat().st_size > 0 else "EMPTY"
            stored = store.read_text(ref)
            checks["PLAN readable"] = "ok" if stored.strip() else "EMPTY"

        # 4. Step 1 SUCCEEDED
        checks["step1 SUCCEEDED"] = (
            "ok" if plan.steps[0].state == WorkflowStepState.SUCCEEDED else f"got {plan.steps[0].state.value}"
        )
        # 5. Step 2 AWAITING_CONFIRMATION
        checks["step2 AWAITING_CONFIRMATION"] = (
            "ok"
            if plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
            else f"got {plan.steps[1].state.value}"
        )
        # 6. Step 2 not started
        checks["step2 NOT started"] = "ok" if plan.steps[1].state != WorkflowStepState.RUNNING else "RUNNING!"
        checks["workflow WAITING_FOR_USER"] = (
            "ok" if plan.state == WorkflowState.WAITING_FOR_USER else f"got {plan.state.value}"
        )
        # 10. process exit clean
        checks["process exit clean"] = "ok" if exit_code and exit_code[-1] == 0 else f"exit={exit_code}"

        # 7. sessions.json unchanged
        sessions_after = SESSIONS_FILE.read_bytes() if SESSIONS_FILE.exists() else None
        checks["sessions.json unchanged"] = "ok" if sessions_after == sessions_before else "CHANGED"
        # 8. claude.json lifecycle untouched
        claude_after = CLAUDE_SOURCE.read_bytes() if CLAUDE_SOURCE.exists() else None
        checks["claude.json untouched"] = "ok" if claude_after == claude_before else "CHANGED"
        # 9. workspace unchanged (allow Claude's own .claude metadata only)
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

        print(f"workflow_id={plan.workflow_id}")
        for name, status in checks.items():
            print(f"  [{'OK' if status == 'ok' else '!!'}] {name}: {status}")
        if plan.steps[0].state == WorkflowStepState.SUCCEEDED:
            print(f"--- PLAN excerpt ---\n{store.read_text(refs[0])[:600]}")

        ok = all(v == "ok" for v in checks.values()) and plan.state == WorkflowState.WAITING_FOR_USER
        store.remove_workflow(plan.workflow_id)  # cleanup only the smoke artifact
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
