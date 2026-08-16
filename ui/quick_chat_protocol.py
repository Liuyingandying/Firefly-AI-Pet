"""Qt-free helpers for Firefly Quick Ask persistent conversations.

The GUI launches Codex/Claude through their CLIs. Provider protocol parsing is
now owned by the unified adapters in ``core.agent_adapters``; the pure
``parse_claude_event`` / ``parse_codex_event`` functions below remain as
backward-compatibility wrappers that delegate to the adapters so Phase 7B tests
and legacy callers keep working unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from core.agent_adapters import ClaudeStreamAdapter, CodexJsonlAdapter
from core.agent_events import AgentEventType


VALID_EFFORTS = {"low", "medium", "high"}

# Per-invocation model for workflow Claude steps (Plan/Review). "opus" is a CLI
# alias resolved through the user's provider env mapping (ANTHROPIC_DEFAULT_OPUS_MODEL
# -> the capable tier). Ordinary Short Talk keeps the default model: the alias
# is only applied when a caller passes model=<this constant> explicitly.
WORKFLOW_CLAUDE_MODEL = "opus"

# Built-in read-only tools exposed to Workflow Plan/Review via --tools. These
# Claude steps may only inspect the workspace, never modify it, so we constrain
# the tool set directly instead of --permission-mode plan (whose Plan system
# context conflicts with Anthropic-compatible third-party backends).
WORKFLOW_READ_ONLY_TOOLS = ("Read", "Glob", "Grep")


@dataclass
class ParsedEvent:
    session_id: str | None = None
    status: str | None = None
    delta: str | None = None
    final: str | None = None
    error: str | None = None


def _checked_effort(effort: str) -> str:
    value = (effort or "low").strip().lower()
    return value if value in VALID_EFFORTS else "low"


def workspace_key(agent: str, workspace: Path | str) -> str:
    path = str(Path(workspace).expanduser().resolve())
    return f"{agent.lower()}|{os.path.normcase(os.path.normpath(path))}"


class SessionRegistry:
    """In-memory session IDs, isolated by agent and workspace."""

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}

    def get(self, agent: str, workspace: Path | str) -> str | None:
        return self._sessions.get(workspace_key(agent, workspace))

    def set(self, agent: str, workspace: Path | str, session_id: str | None) -> None:
        key = workspace_key(agent, workspace)
        if session_id:
            self._sessions[key] = session_id
        else:
            self._sessions.pop(key, None)

    def clear(self, agent: str | None = None, workspace: Path | str | None = None) -> None:
        if agent is None and workspace is None:
            self._sessions.clear()
            return
        for key in list(self._sessions):
            key_agent, _, key_workspace = key.partition("|")
            if agent is not None and key_agent != agent.lower():
                continue
            if workspace is not None:
                wanted = os.path.normcase(os.path.normpath(str(Path(workspace).expanduser().resolve())))
                if key_workspace != wanted:
                    continue
            self._sessions.pop(key, None)


def build_codex_args(
    prompt: str,
    workspace: Path | str,
    *,
    effort: str = "low",
    session_id: str | None = None,
    persistent: bool = True,
    sandbox: str = "read-only",
    reasoning_effort: bool = True,
) -> list[str]:
    """Build Codex non-interactive argv after the executable name.

    Persistent mode stores Codex's exec session and resumes it on later turns.
    Non-persistent mode uses --ephemeral. ``sandbox`` defaults to read-only
    (the managed Short Talk / Quick Ask contract); the workflow Implement step
    explicitly passes ``workspace-write`` so Codex can modify the workspace.

    ``reasoning_effort`` emits the ``-c model_reasoning_effort=...`` override
    (the Short Talk contract). The workflow managed Implement exec passes
    ``reasoning_effort=False`` so Codex runs at its default reasoning effort
    instead of forcing ``low``.
    """
    workspace = Path(workspace)
    args = [
        "exec",
        "--sandbox",
        sandbox,
        "--json",
    ]
    if reasoning_effort:
        args.extend(["-c", f"model_reasoning_effort={_checked_effort(effort)}"])
    if not (workspace / ".git").exists():
        args.append("--skip-git-repo-check")

    if persistent and session_id:
        args.extend(["resume", session_id, prompt])
    else:
        if not persistent:
            args.append("--ephemeral")
        args.append(prompt)
    return args


def build_claude_args(
    prompt: str,
    *,
    effort: str = "low",
    session_id: str | None = None,
    persistent: bool = True,
    isolated: bool = False,
    model: str | None = None,
    read_only_tools: bool = False,
    include_effort: bool = True,
) -> list[str]:
    """Build Claude print-mode argv after the executable name.

    `isolated=True` marks this as a Firefly-internal ask: `--safe-mode`
    disables global lifecycle hooks for this invocation only, so the internal
    ask never pollutes the user's external lifecycle source file. The user's
    own Claude Code sessions (without this flag) keep firing hooks normally.

    `model` is an optional per-invocation model alias (e.g. ``opus`` for
    workflow Plan/Review). When None the CLI uses the caller's default model,
    so ordinary Short Talk semantics are unchanged.

    `read_only_tools=True` (Workflow Plan/Review only) swaps the default
    `--permission-mode plan` for an explicit `--tools` allowlist of the read-only
    built-in tools. Ordinary Short Talk keeps `--permission-mode plan` unchanged.

    `include_effort=False` (Workflow Plan/Review only) omits the explicit
    `--effort` flag so the CLI runs at its default effort. Ordinary Short Talk
    keeps `--effort <low|medium|high>` unchanged.
    """
    args = ["-p"]
    if include_effort:
        args.extend(["--effort", _checked_effort(effort)])
    args.extend([
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ])
    if read_only_tools:
        args.extend(["--tools", ",".join(WORKFLOW_READ_ONLY_TOOLS)])
    else:
        args.extend(["--permission-mode", "plan"])
    if isolated:
        args.append("--safe-mode")
    if model:
        args.extend(["--model", model])
    if persistent and session_id:
        args.extend(["--resume", session_id])
    elif not persistent:
        args.append("--no-session-persistence")
    args.append(prompt)
    return args


# A Short Ask should stay a short ask. Prompts clearly requesting code changes,
# complex tooling, long work, or approvals are routed to the native agent.
_COMPLEX_HINTS = (
    "fix",
    "edit",
    "rewrite",
    "change",
    "update",
    "refactor",
    "implement",
    "write a",
    "write an",
    "create",
    "add a",
    "add an",
    "run ",
    "install",
    "debug this",
    "migrate",
    "test this",
    "deploy",
    "approve",
    "permission",
)


def classify_short_ask(prompt: str) -> str:
    """Return \"simple\" (safe to run as Short Ask) or \"complex\" (recommend native).

    Cheap keyword heuristic only — never blocks; the UI decides how to react.
    """
    lowered = (prompt or "").strip().lower()
    if not lowered:
        return "simple"
    if any(hint in lowered for hint in _COMPLEX_HINTS):
        return "complex"
    return "simple"


def parse_json_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parsed_from_events(events) -> ParsedEvent:
    """Map AgentEvents back to the legacy ParsedEvent shape."""
    parsed = ParsedEvent()
    for ev in events:
        if ev.type == AgentEventType.SESSION and not parsed.session_id:
            parsed.session_id = ev.session_id
        elif ev.type == AgentEventType.STATUS and parsed.status is None:
            parsed.status = ev.status
        elif ev.type == AgentEventType.TEXT_DELTA and parsed.delta is None:
            parsed.delta = ev.text
        elif ev.type == AgentEventType.FINAL and parsed.final is None:
            parsed.final = ev.text
        elif ev.type == AgentEventType.ERROR and parsed.error is None:
            parsed.error = ev.text
    return parsed


def parse_codex_event(event: dict) -> ParsedEvent:
    """Backward-compat wrapper: delegate to the Codex AgentEvent adapter."""
    return _parsed_from_events(CodexJsonlAdapter().feed_event(event))


def parse_claude_event(event: dict) -> ParsedEvent:
    """Backward-compat wrapper: delegate to the Claude AgentEvent adapter."""
    return _parsed_from_events(ClaudeStreamAdapter().feed_event(event))
