"""Phase 9D.6-H4 — direct Plan provider smoke (ONE online call).

Runs the production Workflow Plan path end-to-end through the real
:class:`DirectProviderRunner` (stdlib urllib → the active Anthropic-compatible
provider), the real ``build_plan_prompt``, the real :class:`PlanValidation`, the
real :class:`ArtifactStore`, and the real :class:`WorkflowCoordinator`. It does
NOT touch the Claude Code CLI, ``QuickAskRunner``, ``run_cli.ps1``,
``config/sessions.json``, or any Claude hook.

Exactly ONE provider HTTP call is made. The task is fixed:

    Fix greeting.py so the existing test passes.
    Do not modify test_greeting.py.

The smoke reports VALID/INVALID and whether the PLAN artifact was written and
Step 1 reached SUCCEEDED with Step 2 AWAITING_CONFIRMATION. It never prints the
auth token or any credential; the config is shown redacted.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEventType
from core.artifact_store import ArtifactStore
from core.plan_validation import validate_plan_text
from core.provider_client import (
    ProviderConfigurationError,
    load_workflow_provider_config,
    redact_token,
)
from core.routing_models import TaskRequest
from core.workflow_coordinator import WorkflowCoordinator
from core.workflow_models import ArtifactKind, WorkflowState, WorkflowStepState
from ui.workflow_executor import PlanStepExecutor

TASK = "Fix greeting.py so the existing test passes.\nDo not modify test_greeting.py."


def main() -> int:
    app = QApplication.instance() or QApplication([])

    config_summary = {}
    try:
        config = load_workflow_provider_config()
        config_summary = {
            "base_url": config.base_url,
            "endpoint": config.endpoint,
            "model": config.model,
            "auth": f"credential loaded ({redact_token(config.auth_token)}); value hidden",
        }
    except ProviderConfigurationError as exc:
        config_summary = {"error": str(exc)}

    request = TaskRequest(text=TASK)
    workspace = tempfile.mkdtemp(prefix="fap_h4_smoke_ws_")
    artifact_root = Path(tempfile.mkdtemp(prefix="fap_h4_artifacts_"))

    store = ArtifactStore(artifact_root)
    coord = WorkflowCoordinator()
    plan = coord.create_plan_implement_review(TaskRequest(text=TASK, workspace=workspace))
    plan = coord.confirm_step(plan, plan.steps[0].step_id)

    executor = PlanStepExecutor(coord, store)
    agent_events: list[str] = []
    executor.agent_event.connect(lambda ev: agent_events.append(ev.type.value))

    loop = QEventLoop()

    def on_finished():
        loop.quit()

    executor.turn_finished.connect(on_finished)

    run_error: dict = {}

    def start():
        try:
            executor.execute(plan, "step_1")
        except Exception as exc:  # noqa: BLE001
            run_error["start"] = f"{type(exc).__name__}: {exc}"
            loop.quit()

    QTimer.singleShot(0, start)
    QTimer.singleShot(600_000, loop.quit)  # safety timeout; matches provider timeout
    loop.exec()

    final = executor.plan
    step1 = final.steps[0]
    step2 = final.steps[1]

    plan_text = ""
    validation = validate_plan_text("", TASK)
    if step1.state == WorkflowStepState.SUCCEEDED:
        ref = next((a for a in step1.attached_artifacts if a.kind == ArtifactKind.PLAN), None)
        if ref is not None:
            try:
                plan_text = store.read_text(ref)
            except Exception:
                plan_text = ""
        validation = validate_plan_text(plan_text, TASK)

    report = {
        "config": config_summary,
        "agent_events": agent_events,
        "start_error": run_error.get("start"),
        "plan_length": len(plan_text),
        "plan_first_400": plan_text[:400],
        "contains_greeting_py": "greeting.py" in plan_text.lower(),
        "contains_test_greeting_py": "test_greeting.py" in plan_text.lower(),
        "plan_validation": {
            "valid": validation.valid,
            "reason_codes": [c.value for c in validation.reason_codes],
            "matched_task_terms": list(validation.matched_task_terms),
            "length": validation.length,
        },
        "step1_state": step1.state.value,
        "step2_state": step2.state.value,
        "workflow_state": final.state.value,
        "plan_artifact_written": (artifact_root / final.workflow_id / "plan.md").exists(),
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))

    out_path = PROJECT_DIR / "runtime" / "smoke_h4_plan_direct_result.json"
    try:
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n[saved sanitized report -> {out_path}]")
    except OSError:
        pass

    ok = (
        validation.valid
        and report["plan_artifact_written"]
        and step1.state == WorkflowStepState.SUCCEEDED
        and step2.state == WorkflowStepState.AWAITING_CONFIRMATION
    )
    print(f"\nSMOKE RESULT: {'PASS' if ok else 'FAIL'} (PlanValidation {'VALID' if validation.valid else 'INVALID'})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
