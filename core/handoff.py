"""Phase 9C user-confirmed native handoff models (Qt-free, in-memory).

Handoff is an application-layer handover: Firefly's recommendation is handed
to a native interactive Agent through the Agent's own CLI. Its lifecycle ends
at "successfully handed to the Agent" — never at task success. Task completion
belongs to the external lifecycle (hooks / StateMonitor), never to a
HandoffState.

Nothing here imports Qt, spawns a process, touches the network, or persists a
transcript. HandoffRequests stay in memory; the only optional persistence is a
minimal allowlisted telemetry file that never contains the prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
METRICS_DIR = PROJECT_DIR / "runtime" / "metrics"
HANDOFF_LATEST = METRICS_DIR / "handoff_latest.json"


class HandoffState(str, Enum):
    """Lifecycle of the handoff *transport*, not the agent task.

    PENDING -> LAUNCHING -> HANDED_OFF | FAILED | CANCELLED.
    There is deliberately no "task success" state: HANDED_OFF means the native
    process started and the task payload was handed over.
    """

    PENDING = "pending"
    LAUNCHING = "launching"
    HANDED_OFF = "handed_off"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class HandoffRequest:
    """One user-confirmed handoff. In-memory only; never a transcript store."""

    handoff_id: str
    agent_id: str
    workspace: str
    text: str
    created_at: int
    source: str
    requires_confirmation: bool


def make_handoff_request(
    agent_id: str,
    workspace: str,
    text: str,
    *,
    source: str = "recommendation",
    requires_confirmation: bool = True,
) -> HandoffRequest:
    return HandoffRequest(
        handoff_id=uuid.uuid4().hex[:12],
        agent_id=str(agent_id).lower(),
        workspace=str(workspace),
        text=text,
        created_at=int(time.time() * 1000),
        source=source,
        requires_confirmation=requires_confirmation,
    )


# Reserved first-positional subcommands of each native CLI (from local
# `claude --help` / `codex --help`). A prompt that exactly equals one of these
# would be parsed as a subcommand instead of a task.
_CLAUDE_SUBCOMMANDS = frozenset(
    {
        "agents", "auth", "auto-mode", "doctor", "gateway", "import", "install",
        "mcp", "plugin", "plugins", "project", "setup-token", "ultrareview",
        "update", "upgrade", "help",
    }
)
_CODEX_SUBCOMMANDS = frozenset(
    {
        "exec", "e", "review", "login", "logout", "mcp", "plugin", "mcp-server",
        "app-server", "remote-control", "app", "completion", "update", "doctor",
        "sandbox", "debug", "apply", "a", "resume", "archive", "delete",
        "unarchive", "fork", "cloud", "exec-server", "features", "help",
    }
)


def validate_handoff_prompt(agent_id: str, text: str) -> str | None:
    """Return an error string if `text` cannot be delivered natively as one
    positional argv item, else None.

    Guards the two real CLI-grammar hazards:
      * a prompt that starts with "-" would be parsed as a CLI flag (e.g.
        ``--dangerously-skip-permissions``) instead of a task;
      * a prompt that exactly equals a subcommand name would be parsed as a
        subcommand (e.g. ``exec``) instead of a task.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "Empty prompt cannot be handed off."
    if stripped.startswith("-"):
        return (
            "This prompt starts with '-' and the native CLI would read it as a "
            "flag. Use Open only."
        )
    reserved = _CLAUDE_SUBCOMMANDS if str(agent_id).lower() == "claude" else _CODEX_SUBCOMMANDS
    if stripped.lower() in reserved:
        display = "Claude Code" if str(agent_id).lower() == "claude" else "Codex"
        return (
            f"'{stripped}' is a {display} subcommand, not a task. Use Open only."
        )
    return None


def handoff_hash(handoff_id: str, length: int = 12) -> str:
    """Short, non-reversible identifier for a handoff id (telemetry only)."""
    return hashlib.sha256((handoff_id or "").encode("utf-8", "replace")).hexdigest()[:length]


def workspace_token(workspace: str) -> str:
    """Short diagnostic token: last path segment, lowercased (telemetry only)."""
    name = Path(workspace).name or str(Path(workspace)).replace(os.sep, "_")
    return name.lower()[:40]


@dataclass
class HandoffTelemetry:
    """Minimal allowlisted diagnostics for one handoff attempt.

    Strictly excludes: the prompt, the full workspace path, session ids, API
    keys, auth data, and environment dumps.
    """

    handoff_hash: str
    agent: str
    workspace: str
    transport: str = "native_interactive_argv"
    created_at: int = 0
    launched_at: int | None = None
    duration_ms: int | None = None
    state: str = ""

    def to_payload(self) -> dict:
        return {
            "handoff_hash": self.handoff_hash,
            "agent": self.agent,
            "workspace": self.workspace,
            "transport": self.transport,
            "created_at": self.created_at,
            "launched_at": self.launched_at,
            "duration_ms": self.duration_ms,
            "state": self.state,
        }

    def validate_safe(self) -> list[str]:
        """Return problems if any sensitive value leaked into the payload."""
        problems: list[str] = []
        payload = self.to_payload()
        for key, value in payload.items():
            if not isinstance(value, (str, int, float, bool, type(None))):
                problems.append(f"non-serializable field: {key}")
        for sensitive in ("prompt", "text", "response", "api_key", "token", "env", "password"):
            for key in payload:
                if sensitive in str(key).lower():
                    problems.append(f"sensitive key present: {key}")
        if len(self.handoff_hash) > 16 or not all(
            c in "0123456789abcdef" for c in self.handoff_hash
        ):
            problems.append("handoff_hash not a short hex prefix")
        return problems


class HandoffMetrics:
    """Persist handoff diagnostics to runtime/metrics/handoff_latest.json.

    Latest-file only (no ring buffer): handoff is a single-shot application
    event, not a latency stream.
    """

    def __init__(self, metrics_dir: Path | str = METRICS_DIR) -> None:
        self.latest_file = Path(metrics_dir) / "handoff_latest.json"

    def record(self, telemetry: HandoffTelemetry) -> dict:
        payload = telemetry.to_payload()
        try:
            self.latest_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.latest_file.with_name(self.latest_file.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.latest_file)
        except OSError:
            pass  # telemetry must never break a handoff
        return payload
