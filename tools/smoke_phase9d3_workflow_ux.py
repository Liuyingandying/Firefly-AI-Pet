"""Phase 9D.3 — Online smoke: one real Claude Plan call through the real app layer.

Drives the actual VisualShell integration: a coding prompt routes to the Codex
recommendation card, "Plan with Claude" creates + confirms the
PLAN_IMPLEMENT_REVIEW workflow, and the real PlanStepExecutor runs one managed
Claude Plan call (read-only: --permission-mode plan + --safe-mode). Uses a safe
temporary workspace and a temporary artifact root; the user's real workspace,
sessions, and lifecycle sources are never touched.

Verifies the 11 required outcomes (spec section 39):
  1. the CURRENT final build_plan_prompt() is used (not a stale string)
  2. Step 1 RUNNING right after the click
  3. real Claude output non-empty
  4. plan.md generated under the artifact root
  5. Step 1 SUCCEEDED
  6. Step 2 AWAITING_CONFIRMATION
  7. Step 2 NOT executed (no Codex)
  8. config/sessions.json byte-identical
  9. runtime/sources/claude.json (external lifecycle) untouched
 10. temp workspace unchanged (read-only contract held)
 11. WorkflowCard state correct (Plan ready / Waiting for confirmation / Pending)

Usage: python tools/smoke_phase9d3_workflow_ux.py
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from core.artifact_store import ArtifactStore
from core.workflow_models import ArtifactKind, WorkflowState, WorkflowStepState

SESSIONS_FILE = PROJECT_DIR / "config" / "sessions.json"
CLAUDE_SOURCE = PROJECT_DIR / "runtime" / "sources" / "claude.json"
ARTIFACT_ROOT = PROJECT_DIR / "runtime" / "artifacts"
TIMEOUT_MS = 180_000

TASK = "Create a plan for adding a greeting function to a small Python file. Do not modify files."


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


def _workspace_snapshot(ws: Path) -> dict[str, bytes]:
    return _snapshot(ws)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    checks: dict[str, str] = {}
    exit_code: list[int] = []
    tmp_artifacts = Path(tempfile.mkdtemp(prefix="fap9d3_smoke_artifacts_"))

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        marker = ws / "hello.py"
        marker.write_text("# user file that must not change\ndef hi():\n    return 42\n", encoding="utf-8")
        readme = ws / "README.md"
        readme.write_text("# Safe workspace for the 9D.3 smoke\n", encoding="utf-8")

        files_before = _workspace_snapshot(ws)
        sessions_before = SESSIONS_FILE.read_bytes() if SESSIONS_FILE.exists() else None
        claude_before = CLAUDE_SOURCE.read_bytes() if CLAUDE_SOURCE.exists() else None

        from app import VisualShell

        shell = VisualShell(
            None,
            artifact_root=tmp_artifacts,
            workspace_settings_file=Path(tempfile.mkdtemp(prefix="fap9d3_ws_settings_")) / "ui_settings.json",
        )
        try:
            shell.short_ask.reset()
            shell.recommendation_card.clear_pending()
            shell.dock.select_agent("claude", emit_signal=False)
            shell.workspace_manager.set_current(ws)

            # 1. verify the CURRENT final build_plan_prompt() is what the workflow uses.
            from core.workflow_prompt import build_plan_prompt as _real_prompt

            recorded: list[str] = []

            def _recording_prompt(request):
                prompt = _real_prompt(request)
                recorded.append(prompt)
                return prompt

            with patch("ui.workflow_executor.build_plan_prompt", _recording_prompt):
                shell._on_short_ask_requested()
                shell._on_short_ask_send(TASK)
                card = shell.recommendation_card
                assert card.pending_agent == "codex"
                checks["codex card shown"] = "ok"

                shell.plan_executor._runner.finished.connect(
                    lambda _text, code: exit_code.append(int(code))
                )
                loop = QEventLoop()
                shell.plan_executor.turn_finished.connect(loop.quit)
                timer = QTimer()
                timer.setSingleShot(True)
                timer.timeout.connect(loop.quit)

                card._secondary_btn.click()  # Plan with Claude -> real executor
                running = shell.workflow_coordinator.get_plan(shell.workflow_card.workflow_id)
                # 2. Step 1 RUNNING right after the click.
                checks["step1 RUNNING after click"] = (
                    "ok"
                    if running.steps[0].state == WorkflowStepState.RUNNING
                    else f"got {running.steps[0].state.value}"
                )
                checks["workflow card visible"] = "ok" if shell.workflow_card.isVisible() else "hidden"
                checks["card shows Planning"] = (
                    "ok"
                    if shell.workflow_card._rows[0]._status.text() == "Planning…"
                    else shell.workflow_card._rows[0]._status.text()
                )
                timer.start(TIMEOUT_MS)
                loop.exec()

                wfid = shell.workflow_card.workflow_id
                plan = shell.workflow_coordinator.get_plan(wfid)
                if not exit_code:
                    print("TIMEOUT: the managed Claude call did not finish in time.")
                    return 1

                # 1. current final prompt used
                checks["current build_plan_prompt used"] = (
                    "ok" if recorded and TASK in recorded[0] else "STALE/MISSING"
                )

                # 3. real Claude output non-empty
                text = shell.plan_executor._final_text or "".join(shell.plan_executor._collected_deltas)
                checks["claude output non-empty"] = "ok" if text.strip() else "EMPTY"

                # 4-5. plan.md + Step 1 SUCCEEDED
                refs = plan.steps[0].attached_artifacts
                checks["PLAN artifact attached"] = "ok" if len(refs) == 1 else f"count={len(refs)}"
                ref = refs[0] if refs else None
                if ref is not None:
                    plan_file = tmp_artifacts / ref.path
                    checks["PLAN kind"] = "ok" if ref.kind == ArtifactKind.PLAN else f"got {ref.kind}"
                    checks["plan.md inside artifact root"] = (
                        "ok" if plan_file.resolve().is_relative_to(tmp_artifacts.resolve()) else "ESCAPED"
                    )
                    checks["plan.md exists"] = "ok" if plan_file.is_file() else "MISSING"
                    checks["plan.md non-empty"] = "ok" if plan_file.stat().st_size > 0 else "EMPTY"
                checks["step1 SUCCEEDED"] = (
                    "ok" if plan.steps[0].state == WorkflowStepState.SUCCEEDED else f"got {plan.steps[0].state.value}"
                )

                # 6-7. Step 2 awaiting, not executed.
                checks["step2 AWAITING_CONFIRMATION"] = (
                    "ok"
                    if plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
                    else f"got {plan.steps[1].state.value}"
                )
                checks["step2 NOT executed"] = (
                    "ok" if plan.steps[1].state != WorkflowStepState.RUNNING else "RUNNING!"
                )
                checks["workflow WAITING_FOR_USER"] = (
                    "ok" if plan.state == WorkflowState.WAITING_FOR_USER else f"got {plan.state.value}"
                )

                # 11. WorkflowCard state correct.
                checks["card Plan ready"] = (
                    "ok" if shell.workflow_card._rows[0]._status.text() == "Plan ready" else shell.workflow_card._rows[0]._status.text()
                )
                checks["card Implement waiting"] = (
                    "ok"
                    if shell.workflow_card._rows[1]._status.text() == "Waiting for confirmation"
                    else shell.workflow_card._rows[1]._status.text()
                )
                checks["card Review pending"] = (
                    "ok" if shell.workflow_card._rows[2]._status.text() == "Pending" else shell.workflow_card._rows[2]._status.text()
                )

                # 8-10. isolation.
                sessions_after = SESSIONS_FILE.read_bytes() if SESSIONS_FILE.exists() else None
                checks["sessions.json unchanged"] = "ok" if sessions_after == sessions_before else "CHANGED"
                claude_after = CLAUDE_SOURCE.read_bytes() if CLAUDE_SOURCE.exists() else None
                checks["claude.json untouched"] = "ok" if claude_after == claude_before else "CHANGED"
                files_after = _workspace_snapshot(ws)
                changed = {
                    rel
                    for rel in set(files_before) | set(files_after)
                    if files_before.get(rel) != files_after.get(rel)
                }
                task_changed = [rel for rel in changed if not rel.startswith(".claude/")]
                checks["workspace unchanged"] = "ok" if not task_changed else f"CHANGED: {task_changed[:5]}"
                checks["process exit clean"] = "ok" if exit_code and exit_code[-1] == 0 else f"exit={exit_code}"

            print(f"workflow_id={wfid}")
            for name, status in checks.items():
                print(f"  [{'OK' if status == 'ok' else '!!'}] {name}: {status}")
            if refs:
                print(f"--- PLAN excerpt ---\n{(tmp_artifacts / refs[0].path).read_text(encoding='utf-8')[:600]}")
            ok = all(v == "ok" for v in checks.values())
            print("RESULT:", "PASS" if ok else "FAIL")
            return 0 if ok else 1
        finally:
            shell.shutdown()
            if shell.workflow_card.workflow_id is not None:
                try:
                    ArtifactStore(tmp_artifacts).remove_workflow(shell.workflow_card.workflow_id)
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
