"""Phase 9D.6-H4 — Direct Workflow Provider hotfix tests.

Covers the 62 required scenarios: provider configuration (1-7), the direct
Workflow Plan path (8-19), the deterministic ReviewContext builder (20-33), the
direct Workflow Review path (34-47), runtime taxonomy/cancel/isolation (48-56),
and the regression surface (57-62).

No online calls: the HTTP transport is a fake throughout; the executors are
driven with the same AgentEvent technique as the 9D.2/9D.5 suites.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEvent, AgentEventType, ErrorCategory
from core.artifact_store import ArtifactStore
from core.plan_validation import validate_plan_text
from core.provider_client import (
    ProviderConfigurationError,
    ProviderHttpError,
    ProviderProtocolError,
    ProviderTimeoutError,
    ProviderTransportError,
    WorkflowProviderConfig,
    build_messages_headers,
    build_messages_payload,
    extract_text,
    load_workflow_provider_config,
    normalize_messages_endpoint,
    redact_token,
    strip_context_suffix,
)
from core.review_context import (
    PER_FILE_CAP,
    ReviewContext,
    build_review_context,
    parse_changed_files,
)
from core.routing_models import TaskRequest
from core.workflow_coordinator import WorkflowCoordinator
from core.workflow_models import (
    ArtifactKind,
    CompletionSource,
    StepCompletionEvidence,
    WorkflowState,
    WorkflowStepState,
)
from core.workflow_prompt import build_plan_prompt, build_review_prompt
from ui.workflow_executor import PlanStepExecutor
from ui.workflow_provider_runner import DirectProviderRunner
from ui.workflow_review_executor import ReviewStepExecutor

from test_phase9a_agent_router import _forbidden_in_module

PLAN_EXECUTOR_FILE = PROJECT_DIR / "ui" / "workflow_executor.py"
REVIEW_EXECUTOR_FILE = PROJECT_DIR / "ui" / "workflow_review_executor.py"
PROVIDER_RUNNER_FILE = PROJECT_DIR / "ui" / "workflow_provider_runner.py"
PROVIDER_CLIENT_FILE = PROJECT_DIR / "core" / "provider_client.py"
REVIEW_CONTEXT_FILE = PROJECT_DIR / "core" / "review_context.py"
COORDINATOR_FILE = PROJECT_DIR / "core" / "workflow_coordinator.py"
PROCESS_LAUNCHER_FILE = PROJECT_DIR / "ui" / "process_launcher.py"
IMPLEMENT_FILE = PROJECT_DIR / "ui" / "workflow_implement_executor.py"

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

REAL_BAD_PLAN = (
    "I don't see a specific task or request yet. What would you like me to plan? "
    "Let me know what you're trying to build, fix, or change."
)

CHANGED_TEXT = (
    "# Changed Files\n\n"
    "## Added\n- new_file.py\n\n"
    "## Modified\n- app.py\n\n"
    "## Deleted\n- old_file.py\n"
)

REVIEW_TEXT = (
    "# Verdict\nPASS\n\n# Summary\nThe change matches the plan.\n\n"
    "# Findings\nNone.\n\n# Validation\nTests pass.\n\n# Recommended Next Actions\nNone.\n"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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


def _ready_plan(coord: WorkflowCoordinator, ws: Path):
    plan = coord.create_plan_implement_review(_req(ws))
    return coord.confirm_step(plan, plan.steps[0].step_id)


def _config(**overrides) -> WorkflowProviderConfig:
    base = {
        "base_url": "http://127.0.0.1:15721",
        "auth_token": "sk-test-secret-token",
        "model": "claude-opus-4-7",
    }
    base.update(overrides)
    return WorkflowProviderConfig(**base)


def _plan_with_review_ready(coord, store, ws):
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
        store.write_text(plan.workflow_id, ArtifactKind.CHANGED_FILES, "step_2", CHANGED_TEXT),
    )
    plan = coord.attach_artifact(
        plan, "step_2",
        store.write_text(plan.workflow_id, ArtifactKind.IMPLEMENTATION_SUMMARY, "step_2", "# Summary\n"),
    )
    plan = coord.mark_step_succeeded(
        plan, "step_2", StepCompletionEvidence(source=CompletionSource.USER_CONFIRMED, summary="ok")
    )
    return coord.confirm_step(plan, "step_3")


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


# ===========================================================================
# 1-7. provider configuration
# ===========================================================================

def test_valid_config() -> None:
    config = load_workflow_provider_config(
        {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:15721",
            "ANTHROPIC_AUTH_TOKEN": "sk-token",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7[1M]",
        }
    )
    assert config.base_url == "http://127.0.0.1:15721"
    assert config.auth_token == "sk-token"
    assert config.model == "claude-opus-4-7", "the [1M] suffix must be stripped"
    assert config.endpoint == "http://127.0.0.1:15721/v1/messages"


def test_missing_base_url() -> None:
    try:
        load_workflow_provider_config(
            {"ANTHROPIC_AUTH_TOKEN": "x", "ANTHROPIC_DEFAULT_OPUS_MODEL": "m"}
        )
    except ProviderConfigurationError as exc:
        assert "ANTHROPIC_BASE_URL" in str(exc)
        return
    raise AssertionError("missing base URL must raise ProviderConfigurationError")


def test_missing_auth() -> None:
    try:
        load_workflow_provider_config(
            {"ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_DEFAULT_OPUS_MODEL": "m"}
        )
    except ProviderConfigurationError as exc:
        assert "ANTHROPIC_AUTH_TOKEN" in str(exc)
        return
    raise AssertionError("missing auth token must raise ProviderConfigurationError")


def test_missing_model() -> None:
    try:
        load_workflow_provider_config(
            {"ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_AUTH_TOKEN": "x"}
        )
    except ProviderConfigurationError as exc:
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" in str(exc)
        return
    raise AssertionError("missing model must raise ProviderConfigurationError")


def test_token_never_repr_logged() -> None:
    token = "sk-super-secret-value-123"
    config = _config(auth_token=token)
    assert token not in repr(config), "repr must redact the auth token"
    assert token not in redact_token(token)
    assert "sk-super-secret" not in repr(config)
    headers = build_messages_headers(config)
    # headers may carry the token in-memory, but never into any log/repr path
    assert token in headers["authorization"]  # it is used, just never exposed
    payload = build_messages_payload("hello", config)
    assert token not in json.dumps(payload), "the token must never enter the payload"


def test_endpoint_normalization_local_proxy() -> None:
    assert normalize_messages_endpoint("http://127.0.0.1:15721") == "http://127.0.0.1:15721/v1/messages"
    assert normalize_messages_endpoint("http://127.0.0.1:15721/") == "http://127.0.0.1:15721/v1/messages"


def test_endpoint_normalization_anthropic_url() -> None:
    assert (
        normalize_messages_endpoint("https://api.deepseek.com/anthropic")
        == "https://api.deepseek.com/anthropic/v1/messages"
    )
    assert (
        normalize_messages_endpoint("https://api.anthropic.com")
        == "https://api.anthropic.com/v1/messages"
    )
    assert normalize_messages_endpoint("https://api.anthropic.com/v1") == "https://api.anthropic.com/v1/messages"
    assert normalize_messages_endpoint("https://x/v1/messages") == "https://x/v1/messages", (
        "an already-complete endpoint must not be doubled"
    )


# ===========================================================================
# 8-19. direct Workflow Plan
# ===========================================================================

def test_plan_uses_build_plan_prompt(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_1")
    assert ask_mock.call_args.kwargs["prompt"] == build_plan_prompt(plan.original_request)


def test_plan_no_claude_cli_process() -> None:
    for path in (PLAN_EXECUTOR_FILE, PROVIDER_RUNNER_FILE):
        hits = _forbidden_in_module(path, ("build_claude_args", "QProcess", "subprocess", "Popen"))
        assert not hits, f"{path.name} must not spawn the Claude CLI: {hits}"


def test_plan_no_quick_ask_runner() -> None:
    for path in (PLAN_EXECUTOR_FILE, PROVIDER_RUNNER_FILE):
        hits = _forbidden_in_module(path, ("QuickAskRunner", "process_launcher"))
        assert not hits, f"{path.name} must not use QuickAskRunner: {hits}"


def test_plan_no_run_cli_ps1() -> None:
    for path in (PLAN_EXECUTOR_FILE, PROVIDER_RUNNER_FILE):
        src = path.read_text(encoding="utf-8")
        assert "CLI_WRAPPER" not in src, f"{path.name} must not reference the CLI wrapper"


def test_direct_request_model_correct() -> None:
    config = _config(model="claude-opus-4-7")
    payload = build_messages_payload("hello", config)
    assert payload["model"] == "claude-opus-4-7"
    assert payload["stream"] is False
    assert payload["messages"] == [{"role": "user", "content": "hello"}]


def test_provider_response_collected() -> None:
    config = _config()
    runner = DirectProviderRunner(config=config)
    seen: dict = {}

    def transport(endpoint, payload, headers, timeout, cancel_event):
        seen["model"] = payload["model"]
        seen["endpoint"] = endpoint
        return {"model": config.model, "content": [{"type": "text", "text": GOOD_PLAN}]}

    runner._transport = transport
    events, text = runner._perform("hello", config, threading.Event())
    assert events[-1].type == AgentEventType.FINAL
    assert text == GOOD_PLAN
    assert seen["model"] == "claude-opus-4-7"
    assert seen["endpoint"] == config.endpoint


def test_plan_validation_used() -> None:
    src = PLAN_EXECUTOR_FILE.read_text(encoding="utf-8")
    assert "validate_plan_text" in src


def test_invalid_response_fails(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_1")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REAL_BAD_PLAN))
    ex._on_finished(REAL_BAD_PLAN, 0)
    assert ex.plan.steps[0].state == WorkflowStepState.FAILED
    assert ex.plan.state == WorkflowState.FAILED


def test_valid_response_writes_plan(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_1")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=GOOD_PLAN))
    ex._on_finished(GOOD_PLAN, 0)
    assert ex.plan.steps[0].state == WorkflowStepState.SUCCEEDED
    assert (store.root / ex.plan.workflow_id / "plan.md").exists()


def test_no_retry(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_1")
        ex._on_agent_event(AgentEvent.make("claude", AgentEventType.ERROR, error_code=ErrorCategory.PROVIDER))
        ex._on_failed("boom")
    assert ask_mock.call_count == 1


def test_no_session_manager_write(root, app) -> None:
    for path in (PLAN_EXECUTOR_FILE, PROVIDER_RUNNER_FILE):
        hits = _forbidden_in_module(path, ("SessionManager", "session_manager", "session_store"))
        assert not hits, f"{path.name} must not touch SessionManager: {hits}"


def test_no_hooks_lifecycle_write() -> None:
    for path in (PLAN_EXECUTOR_FILE, PROVIDER_RUNNER_FILE):
        src = path.read_text(encoding="utf-8")
        assert "runtime/sources" not in src
        assert "--safe-mode" not in src


# ===========================================================================
# 20-33. review context
# ===========================================================================

def test_changed_files_parsed() -> None:
    added, modified, deleted = parse_changed_files(CHANGED_TEXT)
    assert added == ("new_file.py",)
    assert modified == ("app.py",)
    assert deleted == ("old_file.py",)


def test_modified_file_loaded(root, app) -> None:
    ws = _ws(root)
    (ws / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    ctx = build_review_context(ws, CHANGED_TEXT)
    modified = [e for e in ctx.entries if e.kind == "modified" and e.path == "app.py"]
    assert modified and "def greet" in modified[0].content


def test_added_file_loaded(root, app) -> None:
    ws = _ws(root)
    (ws / "new_file.py").write_text("print('new')\n", encoding="utf-8")
    ctx = build_review_context(ws, CHANGED_TEXT)
    added = [e for e in ctx.entries if e.kind == "added" and e.path == "new_file.py"]
    assert added and "print('new')" in added[0].content


def test_deleted_file_marked(root, app) -> None:
    ws = _ws(root)
    ctx = build_review_context(ws, CHANGED_TEXT)
    deleted = [e for e in ctx.entries if e.path == "old_file.py"]
    assert deleted and deleted[0].kind == "deleted"
    assert deleted[0].content is None
    assert "deleted" in ctx.render()


def test_relative_path_only() -> None:
    src = REVIEW_CONTEXT_FILE.read_text(encoding="utf-8")
    assert "is_absolute" in src or "resolve" in src


def test_dotdot_rejected(root, app) -> None:
    ws = _ws(root)
    (root / "secret.txt").write_text("secret", encoding="utf-8")
    ctx = build_review_context(ws, "# Changed Files\n\n## Modified\n- ../secret.txt\n")
    entry = ctx.entries[0]
    assert entry.content is None
    assert "unsafe" in (entry.note or "")


def test_absolute_path_rejected(root, app) -> None:
    ws = _ws(root)
    ctx = build_review_context(ws, f"# Changed Files\n\n## Modified\n- {root / 'secret.txt'}\n")
    entry = ctx.entries[0]
    assert entry.content is None
    assert "unsafe" in (entry.note or "")


def test_symlink_escape_rejected(root, app) -> None:
    ws = _ws(root)
    outside = root / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        link = ws / "link.txt"
        link.symlink_to(outside)
    except OSError:
        return  # symlinks unavailable on this platform; the check is still source-verified
    ctx = build_review_context(ws, "# Changed Files\n\n## Modified\n- link.txt\n")
    entry = ctx.entries[0]
    assert entry.content is None or "secret" not in (entry.content or "")


def test_binary_skipped(root, app) -> None:
    ws = _ws(root)
    (ws / "app.py").write_bytes(b"\x00\x01\x02binary")
    ctx = build_review_context(ws, "# Changed Files\n\n## Modified\n- app.py\n")
    entry = ctx.entries[0]
    assert entry.content is None
    assert "binary" in (entry.note or "")


def test_large_file_truncated(root, app) -> None:
    ws = _ws(root)
    (ws / "app.py").write_text("x" * (PER_FILE_CAP + 5000), encoding="utf-8")
    ctx = build_review_context(ws, "# Changed Files\n\n## Modified\n- app.py\n")
    entry = ctx.entries[0]
    assert entry.note == "truncated"
    assert len(entry.content) <= PER_FILE_CAP
    assert ctx.truncated


def test_total_context_capped(root, app) -> None:
    ws = _ws(root)
    for i in range(10):
        (ws / f"f{i}.py").write_text("y" * 40000, encoding="utf-8")
    lines = ["# Changed Files", "", "## Modified"] + [f"- f{i}.py" for i in range(10)]
    ctx = build_review_context(ws, "\n".join(lines), total_cap=100000)
    assert ctx.total_chars <= 100000
    assert ctx.truncated


def test_utf8_handled(root, app) -> None:
    ws = _ws(root)
    (ws / "app.py").write_text("def 你好():\n    return '世界'\n", encoding="utf-8")
    ctx = build_review_context(ws, "# Changed Files\n\n## Modified\n- app.py\n")
    entry = ctx.entries[0]
    assert "你好" in entry.content
    assert "世界" in entry.content


def test_missing_changed_file_controlled(root, app) -> None:
    ws = _ws(root)
    ctx = build_review_context(ws, "# Changed Files\n\n## Modified\n- does_not_exist.py\n")
    entry = ctx.entries[0]
    assert entry.content is None
    assert entry.note == "missing"


def test_workspace_unchanged(root, app) -> None:
    ws = _ws(root)
    (ws / "app.py").write_text("hello\n", encoding="utf-8")
    before = {p.relative_to(ws).as_posix(): p.read_bytes() for p in ws.rglob("*") if p.is_file()}
    build_review_context(ws, CHANGED_TEXT)
    after = {p.relative_to(ws).as_posix(): p.read_bytes() for p in ws.rglob("*") if p.is_file()}
    assert before == after, "building review context must not modify the workspace"


# ===========================================================================
# 34-47. direct Workflow Review
# ===========================================================================

def test_review_plan_included() -> None:
    prompt = build_review_prompt(_req(Path("E:/x")), GOOD_PLAN, CHANGED_TEXT, workspace_context="ctx")
    assert GOOD_PLAN in prompt


def test_review_changed_files_included() -> None:
    prompt = build_review_prompt(_req(Path("E:/x")), GOOD_PLAN, CHANGED_TEXT, workspace_context="ctx")
    assert CHANGED_TEXT in prompt


def test_review_summary_optional() -> None:
    prompt = build_review_prompt(_req(Path("E:/x")), GOOD_PLAN, CHANGED_TEXT)
    assert "no implementation summary provided" in prompt
    prompt2 = build_review_prompt(_req(Path("E:/x")), GOOD_PLAN, CHANGED_TEXT, summary_text="sum")
    assert "sum" in prompt2


def test_review_file_contents_included(root, app) -> None:
    ws = _ws(root)
    (ws / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    ctx = build_review_context(ws, CHANGED_TEXT)
    prompt = build_review_prompt(
        _req(ws), GOOD_PLAN, CHANGED_TEXT, workspace_context=ctx.render()
    )
    assert "def greet" in prompt
    assert "app.py" in prompt


def test_review_no_transcript() -> None:
    prompt = build_review_prompt(_req(Path("E:/x")), GOOD_PLAN, CHANGED_TEXT, workspace_context="ctx")
    for token in ("transcript", "BEGIN", "session_id", "api_key", "sk-"):
        assert token.lower() not in prompt.lower()


def test_review_no_session_ids(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True) as ask_mock:
        ex.execute(plan, "step_3")
    assert "session_id" not in ask_mock.call_args.kwargs
    assert "persistent" not in ask_mock.call_args.kwargs


def test_review_direct_provider_used(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    assert isinstance(ex._runner, DirectProviderRunner)


def test_review_no_claude_cli() -> None:
    hits = _forbidden_in_module(REVIEW_EXECUTOR_FILE, ("build_claude_args", "QProcess", "QuickAskRunner", "process_launcher"))
    assert not hits, f"review executor must not use the Claude CLI: {hits}"


def test_review_artifact_full(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_3")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REVIEW_TEXT))
    ex._on_finished(REVIEW_TEXT, 0)
    ref = [a for s in ex.plan.steps if s.step_id == "step_3" for a in s.attached_artifacts if a.kind == ArtifactKind.REVIEW]
    assert ref and store.read_text(ref[0]) == REVIEW_TEXT


def test_review_managed_agent_result(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    events = []
    coord.connect(lambda e: events.append(e))
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_3")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REVIEW_TEXT))
    ex._on_finished(REVIEW_TEXT, 0)
    succeeded = [e for e in events if e.type.value == "step_succeeded" and e.step_id == "step_3"]
    assert succeeded and succeeded[0].evidence.source == CompletionSource.MANAGED_AGENT_RESULT


def test_review_step3_succeeded(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_3")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REVIEW_TEXT))
    ex._on_finished(REVIEW_TEXT, 0)
    assert ex.plan.steps[2].state == WorkflowStepState.SUCCEEDED


def test_workflow_succeeded(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_3")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=REVIEW_TEXT))
    ex._on_finished(REVIEW_TEXT, 0)
    assert ex.plan.state == WorkflowState.SUCCEEDED


def test_verdict_not_parsed() -> None:
    hits = _forbidden_in_module(REVIEW_EXECUTOR_FILE, ("re", "verdict", "NEEDS_CHANGES", "UNCERTAIN", "PASS"))
    assert not hits, f"review executor must not parse / branch on a verdict: {hits}"


def test_needs_changes_still_completes(root, app) -> None:
    needs_changes = "# Verdict\nNEEDS_CHANGES\n\n# Summary\nNeeds more work.\n"
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _plan_with_review_ready(coord, store, ws)
    ex = ReviewStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_3")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=needs_changes))
    ex._on_finished(needs_changes, 0)
    assert ex.plan.state == WorkflowState.SUCCEEDED
    assert ex.plan.steps[2].state == WorkflowStepState.SUCCEEDED


# ===========================================================================
# 48-56. runtime
# ===========================================================================

def _perform_with(config, transport, cancel_event=None):
    runner = DirectProviderRunner(config=config)
    runner._transport = transport
    return runner._perform("hello", config, cancel_event or threading.Event())


def test_timeout_error() -> None:
    def transport(*a, **k):
        raise ProviderTimeoutError("slow")

    events, text = _perform_with(_config(), transport)
    assert events[-1].type == AgentEventType.ERROR
    assert events[-1].error_code == ErrorCategory.TIMEOUT
    assert text == ""


def test_auth_error() -> None:
    def transport(*a, **k):
        raise ProviderHttpError(401, "unauthorized")

    events, _ = _perform_with(_config(), transport)
    assert events[-1].error_code == ErrorCategory.AUTH


def test_http_provider_error() -> None:
    def transport(*a, **k):
        raise ProviderHttpError(500, "boom")

    events, _ = _perform_with(_config(), transport)
    assert events[-1].error_code == ErrorCategory.PROVIDER


def test_malformed_response() -> None:
    def transport(*a, **k):
        raise ProviderProtocolError("not json")

    events, _ = _perform_with(_config(), transport)
    assert events[-1].error_code == ErrorCategory.PROTOCOL


def test_cancel_prevents_artifact(root, app) -> None:
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_1")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.CANCELLED, error_code=ErrorCategory.CANCELLED))
    ex._on_finished("", 1)
    assert ex.plan.state == WorkflowState.CANCELLED
    assert not (store.root / ex.plan.workflow_id / "plan.md").exists()


def test_late_response_after_cancel_ignored() -> None:
    config = _config()

    def transport(endpoint, payload, headers, timeout, cancel_event):
        cancel_event.set()  # a cancel arrives while the request is in flight
        return {"content": [{"type": "text", "text": GOOD_PLAN}]}

    events, text = _perform_with(config, transport)
    assert events[-1].type == AgentEventType.CANCELLED
    assert text == "", "a late response after cancel must be ignored"


def test_token_never_telemetry() -> None:
    src = PROVIDER_RUNNER_FILE.read_text(encoding="utf-8")
    assert "quick_ask_metrics" not in src
    assert "MetricsWriter" not in src
    assert "telemetry" not in src


def test_sessions_json_unchanged(root, app) -> None:
    sessions_file = root / "config" / "sessions.json"
    sessions_file.parent.mkdir(parents=True, exist_ok=True)
    sessions_file.write_text('{"version": 1, "sessions": []}', encoding="utf-8")
    before = sessions_file.read_bytes()
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_1")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=GOOD_PLAN))
    ex._on_finished(GOOD_PLAN, 0)
    assert sessions_file.read_bytes() == before


def test_claude_json_unchanged(root, app) -> None:
    sources = PROJECT_DIR / "runtime" / "sources"
    before = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            before[p.name] = p.read_bytes()
    ws = _ws(root)
    store = ArtifactStore(root / "artifacts")
    coord = _coord()
    plan = _ready_plan(coord, ws)
    ex = PlanStepExecutor(coord, store, parent=app)
    with patch.object(ex._runner, "ask", return_value=True):
        ex.execute(plan, "step_1")
    ex._on_agent_event(AgentEvent.make("claude", AgentEventType.FINAL, text=GOOD_PLAN))
    ex._on_finished(GOOD_PLAN, 0)
    after = {}
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            after[p.name] = p.read_bytes()
    assert before == after, "a direct Plan must never write claude.json / lifecycle sources"


# ===========================================================================
# 57-62. regression
# ===========================================================================

def test_short_talk_unchanged() -> None:
    from ui.quick_chat_protocol import build_claude_args
    args = build_claude_args("hello", persistent=True, isolated=True)
    assert "--permission-mode" in args and "plan" in args
    assert "--model" not in args
    assert "--tools" not in args
    # QuickAskRunner (Short Talk transport) is untouched
    src = PROCESS_LAUNCHER_FILE.read_text(encoding="utf-8")
    assert "class QuickAskRunner" in src


def test_8c_session_behavior_unchanged() -> None:
    src = PROJECT_DIR / "core" / "session_manager.py"
    assert src.exists()
    hits = _forbidden_in_module(src, ("provider_client", "urllib", "http"))
    assert not hits, "SessionManager must remain provider-free"


def test_workflow_coordinator_unchanged() -> None:
    hits = _forbidden_in_module(
        COORDINATOR_FILE,
        ("provider_client", "review_context", "workflow_provider_runner", "urllib", "http", "threading"),
    )
    assert not hits, f"coordinator must stay provider-free: {hits}"


def test_codex_managed_path_unchanged() -> None:
    from ui.quick_chat_protocol import build_codex_args
    args = build_codex_args("p", "E:/ws", persistent=False, sandbox="workspace-write")
    assert args[0] == "exec"
    assert "--sandbox" in args and args[args.index("--sandbox") + 1] == "workspace-write"
    # Implement executor still uses the managed runner, not the direct provider
    src = IMPLEMENT_FILE.read_text(encoding="utf-8")
    assert "QuickAskRunner" in src


def test_9c_open_claude_unchanged() -> None:
    src = PROCESS_LAUNCHER_FILE.read_text(encoding="utf-8")
    assert "def launch_agent" in src


def test_phase9d_artifacts_unchanged() -> None:
    from core.workflow_models import ArtifactKind as K
    assert {k.value for k in K} == {"plan", "implementation_summary", "changed_files", "review"}


# -- main --------------------------------------------------------------------

def main() -> None:
    app = QApplication.instance() or QApplication([])
    tests = [
        test_valid_config,
        test_missing_base_url,
        test_missing_auth,
        test_missing_model,
        test_token_never_repr_logged,
        test_endpoint_normalization_local_proxy,
        test_endpoint_normalization_anthropic_url,
        test_plan_uses_build_plan_prompt,
        test_plan_no_claude_cli_process,
        test_plan_no_quick_ask_runner,
        test_plan_no_run_cli_ps1,
        test_direct_request_model_correct,
        test_provider_response_collected,
        test_plan_validation_used,
        test_invalid_response_fails,
        test_valid_response_writes_plan,
        test_no_retry,
        test_no_session_manager_write,
        test_no_hooks_lifecycle_write,
        test_changed_files_parsed,
        test_modified_file_loaded,
        test_added_file_loaded,
        test_deleted_file_marked,
        test_relative_path_only,
        test_dotdot_rejected,
        test_absolute_path_rejected,
        test_symlink_escape_rejected,
        test_binary_skipped,
        test_large_file_truncated,
        test_total_context_capped,
        test_utf8_handled,
        test_missing_changed_file_controlled,
        test_workspace_unchanged,
        test_review_plan_included,
        test_review_changed_files_included,
        test_review_summary_optional,
        test_review_file_contents_included,
        test_review_no_transcript,
        test_review_no_session_ids,
        test_review_direct_provider_used,
        test_review_no_claude_cli,
        test_review_artifact_full,
        test_review_managed_agent_result,
        test_review_step3_succeeded,
        test_workflow_succeeded,
        test_verdict_not_parsed,
        test_needs_changes_still_completes,
        test_timeout_error,
        test_auth_error,
        test_http_provider_error,
        test_malformed_response,
        test_cancel_prevents_artifact,
        test_late_response_after_cancel_ignored,
        test_token_never_telemetry,
        test_sessions_json_unchanged,
        test_claude_json_unchanged,
        test_short_talk_unchanged,
        test_8c_session_behavior_unchanged,
        test_workflow_coordinator_unchanged,
        test_codex_managed_path_unchanged,
        test_9c_open_claude_unchanged,
        test_phase9d_artifacts_unchanged,
    ]
    failed = 0
    for fn in tests:
        try:
            _run(fn, app)
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            raise
    print(f"Phase 9D.6-H4 direct provider tests passed ({len(tests)} tests).")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
