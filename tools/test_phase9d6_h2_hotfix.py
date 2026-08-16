"""Phase 9D.6-H2 — Managed Codex Exec + Plan Validation hotfix tests.

Covers the 60 required scenarios: the deterministic Plan validity gate (1-16),
the workflow model pin (17-23), the managed codex exec Implement transport
(24-52), and the staged/resumable D.6 harness contract (56-60). Also asserts the
full existing regression surface that must hold: Plan gate, Short Talk default
model, coordinator confirmation semantics, 9C interactive Open Codex, and no
global config / SessionManager changes.

No online calls: runners are mocked/fake throughout; adapter behavior is driven
directly with synthetic JSONL.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication

from core.agent_adapters import CodexJsonlAdapter, make_adapter
from core.agent_events import (
    STATUS_RECONNECTING,
    AgentEvent,
    AgentEventType,
    ErrorCategory,
)
from core.artifact_store import ArtifactStore
from core.plan_validation import (
    PlanValidationReason,
    validate_plan_text,
)
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
from ui.quick_chat_protocol import (
    WORKFLOW_CLAUDE_MODEL,
    WORKFLOW_READ_ONLY_TOOLS,
    build_claude_args,
    build_codex_args,
)
from ui.process_launcher import QuickAskRunner
from ui.workflow_provider_runner import DirectProviderRunner
from ui.workflow_executor import PlanStepExecutor
from ui.workflow_review_executor import ReviewStepExecutor
from ui.workflow_implement_executor import ImplementStepExecutor

from test_phase9a_agent_router import _forbidden_in_module

PLAN_VALIDATION_FILE = PROJECT_DIR / "core" / "plan_validation.py"
PROCESS_LAUNCHER_FILE = PROJECT_DIR / "ui" / "process_launcher.py"
IMPLEMENT_FILE = PROJECT_DIR / "ui" / "workflow_implement_executor.py"
EXECUTOR_FILE = PROJECT_DIR / "ui" / "workflow_executor.py"
REVIEW_FILE = PROJECT_DIR / "ui" / "workflow_review_executor.py"
STAGED_HARNESS = PROJECT_DIR / "tools" / "smoke_phase9d6_h2_staged.py"

D6_TASK = "Fix greeting.py so the existing test passes. Do not modify test_greeting.py."

REAL_9D6_BAD_PLAN = (
    "I don't see a specific task or request yet. What would you like me to plan? "
    "Let me know what you're trying to build, fix, or change, and I'll investigate "
    "the codebase and put together a plan."
)

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
assert len(GOOD_PLAN) >= 80


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _coord() -> WorkflowCoordinator:
    state = {"t": 1_700_000_000_000}

    def clock() -> int:
        state["t"] += 1
        return state["t"]

    return WorkflowCoordinator(clock=clock)


def _req(ws, text: str = D6_TASK) -> TaskRequest:
    return TaskRequest(text=text, workspace=str(ws))


def _ready_plan(coord: WorkflowCoordinator, ws: Path, text: str = D6_TASK):
    plan = coord.create_plan_implement_review(_req(ws, text))
    return coord.confirm_step(plan, plan.steps[0].step_id)


def _drive_plan_final(ex: PlanStepExecutor, text: str) -> None:
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=text))
    ex._on_finished(text, 0)


def _plan_with_step2_ready(coord: WorkflowCoordinator, store: ArtifactStore, ws: Path):
    """A workflow whose Step 2 is READY with a valid PLAN artifact attached."""
    plan = coord.create_plan_implement_review(_req(ws))
    plan = coord.confirm_step(plan, "step_1")
    plan = coord.mark_step_started(plan, "step_1")
    ref = store.write_text(plan.workflow_id, ArtifactKind.PLAN, "step_1", GOOD_PLAN)
    plan = coord.attach_artifact(plan, "step_1", ref)
    plan = coord.mark_step_succeeded(
        plan, "step_1", StepCompletionEvidence(source=CompletionSource.MANAGED_AGENT_RESULT, summary="ok")
    )
    assert plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    return coord.confirm_step(plan, "step_2")


def _start_impl_ex(coord, store, ws, app):
    plan = _plan_with_step2_ready(coord, store, ws)
    ex = ImplementStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        plan = ex.execute(plan, "step_2")
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    return ex, plan, ask_mock


def _plan_with_review_ready(coord, store, ws):
    """A workflow whose Step 3 is READY (Step 2 completed with its artifacts)."""
    plan = _plan_with_step2_ready(coord, store, ws)  # step_2 READY
    plan = coord.mark_step_started(plan, "step_2")
    plan = coord.attach_artifact(
        plan,
        "step_2",
        store.write_text(
            plan.workflow_id, ArtifactKind.CHANGED_FILES, "step_2",
            "# Changed Files\n\n## Modified\n- greeting.py\n",
        ),
    )
    plan = coord.attach_artifact(
        plan,
        "step_2",
        store.write_text(
            plan.workflow_id, ArtifactKind.IMPLEMENTATION_SUMMARY, "step_2",
            "# Implementation Summary\nTask: x\n",
        ),
    )
    plan = coord.mark_step_succeeded(
        plan, "step_2", StepCompletionEvidence(source=CompletionSource.USER_CONFIRMED, summary="ok")
    )
    assert plan.steps[2].state == WorkflowStepState.AWAITING_CONFIRMATION
    return coord.confirm_step(plan, "step_3")


def _finish_impl(ex: ImplementStepExecutor, text: str = "done", exit_code: int = 0) -> None:
    ex._on_runner_finished(text, exit_code)


def _ws(root: Path) -> Path:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _run(fn, app):
    params = list(inspect.signature(fn).parameters)
    if not params:
        return fn()
    if params == ["root", "app"]:
        with tempfile.TemporaryDirectory() as td:
            return fn(Path(td), app)
    if params == ["app"]:
        return fn(app)
    raise SystemExit(f"unknown signature {fn.__name__}: {params}")


# ===========================================================================
# 1-16. Plan validity gate
# ===========================================================================

def test_empty_plan_invalid() -> None:
    r = validate_plan_text("", D6_TASK)
    assert not r.valid
    assert PlanValidationReason.EMPTY in r.reason_codes
    assert r.length == 0


def test_whitespace_plan_invalid() -> None:
    r = validate_plan_text("   \n\t  ", D6_TASK)
    assert not r.valid
    assert PlanValidationReason.EMPTY in r.reason_codes


def test_too_short_invalid() -> None:
    r = validate_plan_text("Make it work.", D6_TASK)
    assert not r.valid
    assert PlanValidationReason.TOO_SHORT in r.reason_codes


def test_generic_no_task_invalid() -> None:
    r = validate_plan_text(
        "I don't see a specific task. Please provide a task.", D6_TASK
    )
    assert not r.valid
    assert PlanValidationReason.GENERIC_NO_TASK_RESPONSE in r.reason_codes


def test_real_9d6_bad_plan_invalid() -> None:
    r = validate_plan_text(REAL_9D6_BAD_PLAN, D6_TASK)
    assert not r.valid
    assert PlanValidationReason.GENERIC_NO_TASK_RESPONSE in r.reason_codes
    assert PlanValidationReason.NO_TASK_REFERENCE in r.reason_codes
    assert r.matched_task_terms == ()


def test_good_plan_valid() -> None:
    r = validate_plan_text(GOOD_PLAN, D6_TASK)
    assert r.valid
    assert r.reason_codes == ()
    assert "greeting.py" in r.matched_task_terms


def test_file_token_matching() -> None:
    plan = (
        "# Plan\n\nUpdate greeting.py so it interpolates the passed name, then run the tests.\n"
    )
    r = validate_plan_text(plan, "Fix greeting.py")
    assert r.valid
    assert "greeting.py" in r.matched_task_terms


def test_case_handling() -> None:
    plan = (
        "# Plan\n\nUpdate GREETING.PY so the greet function interpolates the passed "
        "name, then verify the existing test suite still passes end to end.\n"
    )
    r = validate_plan_text(plan, "Fix greeting.py")
    assert r.valid
    assert "greeting.py" in r.matched_task_terms


def test_path_like_token_matching() -> None:
    plan = (
        "# Plan\n\nRefactor src/foo.py and update docs/README.md for the new API, "
        "then run the full test suite to confirm nothing regressed.\n"
    )
    r = validate_plan_text(
        plan,
        "Refactor src/foo.py and update docs/README.md.",
    )
    assert r.valid
    assert "src/foo.py" in r.matched_task_terms
    assert "docs/README.md" in r.matched_task_terms


def test_no_file_token_request_not_misjudged() -> None:
    r = validate_plan_text(
        "# Plan\n\nRefactor the login flow into smaller, testable steps and add unit tests.\n",
        "Refactor the login flow.",
    )
    assert r.valid, "a request without file-like tokens must never trigger NO_TASK_REFERENCE"


def test_validation_deterministic() -> None:
    a = validate_plan_text(REAL_9D6_BAD_PLAN, D6_TASK)
    b = validate_plan_text(REAL_9D6_BAD_PLAN, D6_TASK)
    assert a == b
    assert a.valid == b.valid and a.reason_codes == b.reason_codes


def test_gate_no_llm_network() -> None:
    source = PLAN_VALIDATION_FILE.read_text(encoding="utf-8")
    for forbidden in (
        "import requests",
        "import urllib",
        "import socket",
        "import subprocess",
        "openai",
        "anthropic",
        "PySide6",
        "QProcess",
        "QObject",
        "http",
        "def main",
    ):
        assert forbidden not in source, f"plan gate must not use {forbidden!r}"


def test_invalid_plan_step1_fails(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    events: list = []
    coord.connect(lambda e: events.append(e))
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_1")
    _drive_plan_final(ex, REAL_9D6_BAD_PLAN)
    assert ex.plan.steps[0].state == WorkflowStepState.FAILED
    assert ex.plan.state == WorkflowState.FAILED
    failed = [e for e in events if e.type == WorkflowEventType.STEP_FAILED and e.step_id == "step_1"]
    assert failed and failed[0].error_code == "plan_invalid"


def test_invalid_plan_no_plan_artifact(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_1")
    _drive_plan_final(ex, REAL_9D6_BAD_PLAN)
    assert ex.plan.steps[0].attached_artifacts == ()
    assert not (store.root / ex.plan.workflow_id / "plan.md").exists(), (
        "an invalid plan must never be written as a PLAN artifact"
    )


def test_invalid_plan_never_starts_step2(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_1")
    _drive_plan_final(ex, REAL_9D6_BAD_PLAN)
    assert ex.plan.steps[1].state == WorkflowStepState.SKIPPED, "Step 2 must be skipped"
    assert ask_mock.call_count == 1, "an invalid plan must never start Step 2"


def test_invalid_plan_no_auto_retry(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_1")
    _drive_plan_final(ex, REAL_9D6_BAD_PLAN)
    assert ask_mock.call_count == 1, "an invalid plan must not be retried automatically"
    assert ex.plan.state == WorkflowState.FAILED


# ===========================================================================
# 17-23. workflow model pin
# ===========================================================================

def _workflow_claude_args() -> list:
    """The exact argv a Workflow Plan/Review step produces through the runner."""
    return build_claude_args(
        "x",
        persistent=False,
        isolated=True,
        model=WORKFLOW_CLAUDE_MODEL,
        read_only_tools=True,
        include_effort=False,
    )


def test_short_talk_model_semantics_unchanged() -> None:
    args = build_claude_args("x", persistent=True, isolated=True)
    assert "--model" not in args, "Short Talk default must not add a --model"


def test_workflow_plan_direct_provider(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    assert isinstance(ex._runner, DirectProviderRunner)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_1")
    assert set(ask_mock.call_args.kwargs) == {"prompt"}, "plan ask is prompt-only"
    # The stronger model is resolved from provider config, not a --model argv.
    from core.provider_client import load_workflow_provider_config
    config = load_workflow_provider_config(
        {
            "ANTHROPIC_BASE_URL": "http://x",
            "ANTHROPIC_AUTH_TOKEN": "t",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7[1M]",
        }
    )
    assert config.model == "claude-opus-4-7"


def test_workflow_review_direct_provider(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    assert isinstance(ex._runner, DirectProviderRunner)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    assert set(ask_mock.call_args.kwargs) == {"prompt"}, "review ask is prompt-only"


def test_no_global_config_change() -> None:
    assert WORKFLOW_CLAUDE_MODEL == "opus"
    # Only explicit model= calls add --model; nothing writes ~/.claude/settings.json.
    for src in (EXECUTOR_FILE.read_text(encoding="utf-8"), REVIEW_FILE.read_text(encoding="utf-8")):
        assert "settings.json" not in src


def test_no_resume_workflow() -> None:
    args = _workflow_claude_args()
    assert "--resume" not in args
    assert "--no-session-persistence" in args


def test_safe_mode_retained() -> None:
    args = _workflow_claude_args()
    assert "--safe-mode" in args


def test_workflow_read_only_tools_no_permission_mode() -> None:
    # Both Workflow Plan and Workflow Review produce this same argv: an explicit
    # read-only --tools allowlist, never --permission-mode plan.
    args = _workflow_claude_args()
    assert "--permission-mode" not in args
    assert "plan" not in args
    assert "--tools" in args
    tools_arg = args[args.index("--tools") + 1]
    assert set(tools_arg.split(",")) == set(WORKFLOW_READ_ONLY_TOOLS)
    assert set(WORKFLOW_READ_ONLY_TOOLS) == {"Read", "Glob", "Grep"}
    for forbidden in ("Write", "Edit", "Bash", "NotebookEdit"):
        assert forbidden not in args
    assert "--safe-mode" in args
    assert "--model" in args and args[args.index("--model") + 1] == WORKFLOW_CLAUDE_MODEL
    assert "--resume" not in args
    assert "--no-session-persistence" in args


# -- effort omission (Workflow Plan/Review only) -----------------------------
# The workflow Claude steps must not force --effort low: the CLI runs at its
# default effort. Ordinary Short Talk keeps --effort <low|medium|high>.

WORKFLOW_CLAUDE_EXPECTED_ARGS = [
    "-p",
    "--output-format", "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--tools", "Read,Glob,Grep",
    "--safe-mode",
    "--model", "opus",
    "--no-session-persistence",
    "x",
]


def test_workflow_plan_argv_has_no_effort(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_1")
    # The direct provider has no CLI effort/model/session flags at all.
    assert set(ask_mock.call_args.kwargs) == {"prompt"}


def test_workflow_review_argv_has_no_effort(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    assert set(ask_mock.call_args.kwargs) == {"prompt"}


def test_short_talk_effort_unchanged() -> None:
    # Ordinary Short Talk still emits --effort with the caller's value.
    low = build_claude_args("x", persistent=True, isolated=True)
    assert "--effort" in low
    assert low[low.index("--effort") + 1] == "low"
    high = build_claude_args("x", effort="high", persistent=True, isolated=True)
    assert high[high.index("--effort") + 1] == "high"
    # Short Talk keeps the default plan permission mode, not the workflow tools.
    assert "--permission-mode" in low
    assert "--model" not in low


def test_codex_argv_unchanged() -> None:
    # Short Talk Codex still forces its reasoning-effort override...
    short_talk = build_codex_args("prompt", "E:/ws", persistent=False, sandbox="read-only")
    assert "-c" in short_talk
    assert "model_reasoning_effort=low" in short_talk
    # ...and the workflow managed exec still omits it (unchanged by this hotfix).
    managed = build_codex_args(
        "prompt", "E:/ws", persistent=False, sandbox="workspace-write", reasoning_effort=False
    )
    assert not any("model_reasoning_effort" in a for a in managed)
    assert managed[0] == "exec"


# ===========================================================================
# 24-52. codex managed exec
# ===========================================================================

def test_uses_codex_exec() -> None:
    args = build_codex_args("prompt", "E:/ws", persistent=False, sandbox="workspace-write")
    assert args[0] == "exec"


def test_uses_workspace_write_sandbox(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, ask_mock = _start_impl_ex(coord, store, ws, app)
    assert ask_mock.call_args.kwargs["sandbox"] == "workspace-write"
    args = build_codex_args("x", ws, persistent=False, sandbox="workspace-write")
    assert args[args.index("--sandbox") + 1] == "workspace-write"


def test_uses_json(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    _start_impl_ex(coord, store, ws, app)
    args = build_codex_args("x", ws, persistent=False, sandbox="workspace-write")
    assert "--json" in args


def test_uses_ephemeral(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, ask_mock = _start_impl_ex(coord, store, ws, app)
    assert ask_mock.call_args.kwargs["persistent"] is False
    args = build_codex_args("x", ws, persistent=False, sandbox="workspace-write")
    assert "--ephemeral" in args


def test_managed_exec_no_reasoning_effort(root, app) -> None:
    """The workflow managed codex exec must NOT force -c model_reasoning_effort=low."""
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, ask_mock = _start_impl_ex(coord, store, ws, app)
    assert ask_mock.call_args.kwargs.get("reasoning_effort") is False
    args = build_codex_args(
        "prompt", ws, persistent=False, sandbox="workspace-write", reasoning_effort=False
    )
    assert not any("model_reasoning_effort" in a for a in args), (
        "managed exec must not force reasoning_effort=low"
    )
    # retained contract
    assert args[0] == "exec"
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert "--json" in args
    assert "--skip-git-repo-check" in args
    assert "--ephemeral" in args


def test_working_directory_locked(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, ask_mock = _start_impl_ex(coord, store, ws, app)
    assert ask_mock.call_args.kwargs["workspace"] == str(plan.workspace)


def test_no_startDetached_in_implement() -> None:
    hits = _forbidden_in_module(IMPLEMENT_FILE, ("startDetached", "launch_agent"))
    assert not hits, f"executor must not use the detached interactive handoff: {hits}"


def test_qprocess_owned_via_runner(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, ask_mock = _start_impl_ex(coord, store, ws, app)
    assert isinstance(ex._runner, QuickAskRunner), "the executor owns a QuickAskRunner process"
    # ownership: the executor must not shell out itself
    assert not _forbidden_in_module(IMPLEMENT_FILE, ("QProcess", "Popen", "subprocess"))


def test_stdin_closed_for_codex() -> None:
    src = PROCESS_LAUNCHER_FILE.read_text(encoding="utf-8")
    assert "closeWriteChannel" in src, "the runner must close the stdin write channel"
    assert "_agent == \"codex\"" in src, "stdin close must be scoped to the codex managed path"


def test_prompt_single_argv_item_safe_transport() -> None:
    prompt = "Fix greeting.py so the existing test passes."
    args = build_codex_args(prompt, "E:/ws", persistent=False, sandbox="workspace-write")
    assert args[-1] == prompt
    assert args.count(prompt) == 1


def test_existing_codex_adapter_reused() -> None:
    hits = _forbidden_in_module(IMPLEMENT_FILE, ("CodexJsonlAdapter", "feed_line", "make_adapter"))
    assert not hits, f"executor must delegate to the existing adapter: {hits}"
    assert make_adapter("codex").agent_id == "codex"


def test_no_provider_json_parser_duplicate() -> None:
    hits = _forbidden_in_module(IMPLEMENT_FILE, ("json", "loads", "dumps", "raw_event"))
    assert not hits, f"executor must not parse provider JSON itself: {hits}"


def test_session_event_handled() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event({"type": "thread.started", "thread_id": "thr-9"})
    assert any(e.type == AgentEventType.SESSION and e.session_id == "thr-9" for e in events)


def test_status_event_handled() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event({"type": "turn.started"})
    assert any(e.type == AgentEventType.STATUS for e in events)


def test_tool_event_handled() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event({"type": "item.started", "item": {"type": "command_execution"}})
    assert any(e.type == AgentEventType.TOOL and e.tool_name == "command_execution" for e in events)


def test_final_event_handled() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
    )
    assert any(e.type == AgentEventType.FINAL and e.text == "done" for e in events)


def test_fatal_error_handled() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event({"type": "turn.failed", "message": "fatal boom"})
    assert any(e.type == AgentEventType.ERROR for e in events)


def test_reconnect_not_fatal() -> None:
    adapter = CodexJsonlAdapter()
    events = adapter.feed_event(
        {"type": "error", "message": "Reconnecting… 2/5 (request timed out)"}
    )
    assert not any(e.type == AgentEventType.ERROR for e in events), "reconnect is not a fatal error"
    assert any(e.type == AgentEventType.STATUS and e.status == STATUS_RECONNECTING for e in events)
    assert adapter._saw_error is False
    # a genuinely fatal error without reconnect evidence still fails
    events = adapter.feed_event({"type": "error", "error": {"message": "boom"}})
    assert any(e.type == AgentEventType.ERROR for e in events)


def test_exit_code_captured(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    finished: list[int] = []
    ex.execution_finished.connect(lambda code: finished.append(code))
    _finish_impl(ex, "done", 0)
    assert ex.managed_exec_finished is True
    assert ex.managed_exec_exit_code == 0
    assert finished == [0]


def test_exit0_does_not_auto_succeed(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    _finish_impl(ex, "done", 0)
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    assert ex.plan.steps[1].state == WorkflowStepState.RUNNING
    assert ex.plan.steps[1].attached_artifacts == ()
    assert ex.plan.state == WorkflowState.RUNNING


def test_execution_finished_state_exposed(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    assert ex.managed_exec_finished is False
    _finish_impl(ex, "done", 0)
    assert ex.managed_exec_finished is True
    assert ex.managed_exec_exit_code == 0


def test_completion_still_needs_user(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    _finish_impl(ex, "done", 0)
    attempt = ex.confirm_completion(plan, "step_2")
    assert attempt.ok is False and attempt.no_changes is True
    assert ex.plan.steps[1].state == WorkflowStepState.RUNNING


def test_snapshot_evidence_retained(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    _finish_impl(ex, "done", 0)
    (ws / "greeting.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
    attempt = ex.confirm_completion(plan, "step_2")
    assert attempt.ok
    cf = [a for a in ex.plan.steps[1].attached_artifacts if a.kind == ArtifactKind.CHANGED_FILES][0]
    assert "greeting.py" in store.read_text(cf)


def test_user_confirmation_generates_artifacts(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    _finish_impl(ex, "done", 0)
    (ws / "greeting.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
    attempt = ex.confirm_completion(plan, "step_2")
    assert attempt.ok
    kinds = {a.kind for a in ex.plan.steps[1].attached_artifacts}
    assert kinds == {ArtifactKind.CHANGED_FILES, ArtifactKind.IMPLEMENTATION_SUMMARY}
    assert ex.plan.steps[1].state == WorkflowStepState.SUCCEEDED


def test_process_launch_failure_fails_workflow(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_step2_ready(coord, store, ws)
    ex = ImplementStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=False):
        plan = ex.execute(plan, "step_2")
    assert plan.state == WorkflowState.FAILED
    assert plan.steps[1].state == WorkflowStepState.FAILED
    assert plan.steps[2].state == WorkflowStepState.SKIPPED
    assert not ex.running


def test_nonzero_exit_fails(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    _finish_impl(ex, "", 1)
    assert ex.plan.state == WorkflowState.FAILED
    assert ex.plan.steps[1].state == WorkflowStepState.FAILED
    assert ex.plan.steps[1].attached_artifacts == ()


def test_fatal_agent_error_fails(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    ex._on_agent_event(AgentEvent.make("codex", AgentEventType.ERROR, error_code=ErrorCategory.PROVIDER))
    ex._on_runner_failed("fatal")
    assert ex.plan.state == WorkflowState.FAILED
    assert ex.plan.steps[1].state == WorkflowStepState.FAILED


def test_no_fallback_interactive(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, ask_mock = _start_impl_ex(coord, store, ws, app)
    _finish_impl(ex, "", 1)
    assert not _forbidden_in_module(IMPLEMENT_FILE, ("launch_agent", "startDetached")), (
        "executor must have no interactive fallback path"
    )
    assert ask_mock.call_count == 1, "no second execution after a failure"


def test_no_auto_retry(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, ask_mock = _start_impl_ex(coord, store, ws, app)
    _finish_impl(ex, "", 1)
    assert ask_mock.call_count == 1


def test_cancel_owns_only_current_process(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    with patch.object(ex._runner, "stop") as stop_mock:
        ex.stop()
    assert stop_mock.call_count == 1
    src = PROCESS_LAUNCHER_FILE.read_text(encoding="utf-8")
    assert "taskkill.exe" in src and "/PID" in src


def test_no_taskkill_all() -> None:
    src = PROCESS_LAUNCHER_FILE.read_text(encoding="utf-8")
    assert "codex.exe" not in src, "cancel must never taskkill every codex.exe"


# ===========================================================================
# 53-55 + regression: SessionManager / Short Talk / 9C Open Codex
# ===========================================================================

def test_session_manager_unchanged(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    assert ex._runner._sessions._store is None, "transient runner must have no store"
    _finish_impl(ex, "done", 0)
    (ws / "greeting.py").write_text("x", encoding="utf-8")
    ex.confirm_completion(plan, "step_2")
    assert ex._runner._sessions.get_native_id("codex", ws) is None


def test_external_short_talk_unchanged() -> None:
    args = build_claude_args("hello", persistent=True, isolated=True)
    assert "--model" not in args
    # Short Talk keeps its read-only --permission-mode plan contract; the
    # workflow --tools allowlist must not leak into it.
    assert "--permission-mode" in args and "plan" in args
    assert "--tools" not in args
    args_codex = build_codex_args("hello", "E:/ws", persistent=True)
    assert "--sandbox" in args_codex and args_codex[args_codex.index("--sandbox") + 1] == "read-only"
    assert "--tools" not in args_codex
    assert "--permission-mode" not in args_codex


def test_9c_open_codex_unchanged() -> None:
    # Phase 9C interactive launch path is untouched: launch_agent still exists
    # in process_launcher and is not used by the Implement executor.
    from ui.process_launcher import ProcessLauncher

    assert hasattr(ProcessLauncher, "launch_agent")
    src = PROCESS_LAUNCHER_FILE.read_text(encoding="utf-8")
    assert "def launch_agent" in src


def test_confirm_completion_semantics_unchanged(root, app) -> None:
    # USER_CONFIRMED + artifacts is still the only Step-2 success evidence.
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    ex, plan, _ = _start_impl_ex(coord, store, ws, app)
    _finish_impl(ex, "done", 0)
    (ws / "greeting.py").write_text("changed", encoding="utf-8")
    events: list = []
    coord.connect(lambda e: events.append(e))
    ex.confirm_completion(plan, "step_2")
    succeeded = [e for e in events if e.type == WorkflowEventType.STEP_SUCCEEDED and e.step_id == "step_2"]
    assert succeeded and succeeded[0].evidence.source == CompletionSource.USER_CONFIRMED


# ===========================================================================
# 56-60. D6 staged harness
# ===========================================================================

def test_no_infinite_human_gate() -> None:
    src = STAGED_HARNESS.read_text(encoding="utf-8")
    assert "while True" not in src, "the harness must not block on an infinite gate"
    assert "--run-plan-and-implement" in src
    assert "--continue" in src
    assert "--prepare" in src


def test_staged_resume_supported() -> None:
    src = STAGED_HARNESS.read_text(encoding="utf-8")
    assert "def run_plan_and_implement" in src
    assert "def continue_stage" in src


def test_test_only_state_separate_from_production() -> None:
    import tools.smoke_phase9d6_h2_staged as staged

    ws = Path(tempfile.mkdtemp(prefix="fap_h2_state_"))
    state_path = Path(ws) / "state.json"
    staged.save_state({"workspace": str(ws), "baseline": {"records": {}, "skipped": [], "root": str(ws)}}, state_path)
    assert state_path.is_file()
    # the state lives outside production workflow persistence
    assert "config/workflows.json" not in str(state_path)
    assert "runtime/artifacts" not in str(state_path)
    import shutil

    shutil.rmtree(ws, ignore_errors=True)


def test_secrets_not_stored() -> None:
    import tools.smoke_phase9d6_h2_staged as staged

    ws = Path(tempfile.mkdtemp(prefix="fap_h2_sec_"))
    state_path = Path(ws) / "state.json"
    staged.save_state(
        {
            "workspace": str(ws),
            "api_key": "sk-secret",
            "claude_session_id": "sess-x",
            "transcript": "full transcript",
            "baseline": {"records": {}, "skipped": [], "root": str(ws)},
        },
        state_path,
    )
    raw = state_path.read_text(encoding="utf-8")
    for bad in ("sk-secret", "sess-x", "transcript", "api_key"):
        assert bad not in raw, f"test-only state must never store {bad!r}"
    import shutil

    shutil.rmtree(ws, ignore_errors=True)


def test_disposable_workspace_only() -> None:
    import tools.smoke_phase9d6_h2_staged as staged

    assert staged.cleanup(r"C:\Windows\System32") == 1, "must refuse non-temp workspaces"
    assert staged.cleanup(r"C:\does-not-exist-anywhere") == 1


def test_harness_reconstruction_uses_real_transitions(root, app) -> None:
    import tools.smoke_phase9d6_h2_staged as staged

    ws = _ws(root)
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
    assert plan.steps[0].state == WorkflowStepState.SUCCEEDED
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    assert impl_ex.managed_exec_finished is True
    assert impl_ex.managed_exec_exit_code == 0
    # the saved PLAN artifact is attached and readable
    ref = [a for s in plan.steps if s.step_id == "step_1" for a in s.attached_artifacts if a.kind == ArtifactKind.PLAN]
    assert ref and store.read_text(ref[0]) == GOOD_PLAN


# -- main --------------------------------------------------------------------

def main() -> None:
    app = QApplication.instance() or QApplication([])
    tests = [
        test_empty_plan_invalid,
        test_whitespace_plan_invalid,
        test_too_short_invalid,
        test_generic_no_task_invalid,
        test_real_9d6_bad_plan_invalid,
        test_good_plan_valid,
        test_file_token_matching,
        test_case_handling,
        test_path_like_token_matching,
        test_no_file_token_request_not_misjudged,
        test_validation_deterministic,
        test_gate_no_llm_network,
        test_invalid_plan_step1_fails,
        test_invalid_plan_no_plan_artifact,
        test_invalid_plan_never_starts_step2,
        test_invalid_plan_no_auto_retry,
        test_short_talk_model_semantics_unchanged,
        test_workflow_plan_direct_provider,
        test_workflow_review_direct_provider,
        test_no_global_config_change,
        test_no_resume_workflow,
        test_safe_mode_retained,
        test_workflow_read_only_tools_no_permission_mode,
        test_workflow_plan_argv_has_no_effort,
        test_workflow_review_argv_has_no_effort,
        test_short_talk_effort_unchanged,
        test_codex_argv_unchanged,
        test_uses_codex_exec,
        test_uses_workspace_write_sandbox,
        test_uses_json,
        test_uses_ephemeral,
        test_managed_exec_no_reasoning_effort,
        test_working_directory_locked,
        test_no_startDetached_in_implement,
        test_qprocess_owned_via_runner,
        test_stdin_closed_for_codex,
        test_prompt_single_argv_item_safe_transport,
        test_existing_codex_adapter_reused,
        test_no_provider_json_parser_duplicate,
        test_session_event_handled,
        test_status_event_handled,
        test_tool_event_handled,
        test_final_event_handled,
        test_fatal_error_handled,
        test_reconnect_not_fatal,
        test_exit_code_captured,
        test_exit0_does_not_auto_succeed,
        test_execution_finished_state_exposed,
        test_completion_still_needs_user,
        test_snapshot_evidence_retained,
        test_user_confirmation_generates_artifacts,
        test_process_launch_failure_fails_workflow,
        test_nonzero_exit_fails,
        test_fatal_agent_error_fails,
        test_no_fallback_interactive,
        test_no_auto_retry,
        test_cancel_owns_only_current_process,
        test_no_taskkill_all,
        test_session_manager_unchanged,
        test_external_short_talk_unchanged,
        test_9c_open_codex_unchanged,
        test_confirm_completion_semantics_unchanged,
        test_no_infinite_human_gate,
        test_staged_resume_supported,
        test_test_only_state_separate_from_production,
        test_secrets_not_stored,
        test_disposable_workspace_only,
        test_harness_reconstruction_uses_real_transitions,
    ]
    failed = 0
    for fn in tests:
        try:
            _run(fn, app)
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            raise
    print(f"Phase 9D.6-H2 hotfix tests passed ({len(tests)} tests).")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
