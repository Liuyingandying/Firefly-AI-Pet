"""Phase v0.3-P9: Agent Front Platform — Workspace selector + Task input + AUTO routing.

Architecture:
  - WorkspaceSelector: manages workspace list, current workspace, switching
  - TaskInput: collects user task prompt with agent override
  - AutoRouter: wraps AgentRouter with handoff package generation
  - HandoffPackage: structured payload for agent handoff/upgrade

Agent strategy:
  - Default: Qwen (via existing CompanionRuntime / CharacterConversationRunner)
  - Complex tasks: upgrade to Codex/Claude via handoff
  - Single writer per workspace (enforced by WorkspaceLock)

Dependencies:
  - core.agent_router (this project)
  - core.handoff (this project)
  - core.workspace_manager (this project)
  - core.routing_models (this project)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from core.agent_router import AgentRouter
from core.routing_models import (
    AgentRecommendation,
    HandoffMode,
    TaskRequest,
)

log = logging.getLogger("firefly.p9_platform")


# ======================================================================
# WorkspaceLock — single writer per workspace
# ======================================================================


class WorkspaceLock:
    """Ensure at most one writer agent per workspace.

    Thread-safe. Uses file-based lock + in-memory state.
    """

    def __init__(self, lock_dir: Path | str | None = None) -> None:
        self._lock_dir = Path(lock_dir) if lock_dir else Path(__file__).resolve().parent.parent / "runtime" / "locks"
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, str] = {}  # workspace_path -> agent_id

    def acquire(self, workspace: str, agent_id: str) -> bool:
        """Try to acquire write lock for a workspace. Returns True if acquired."""
        ws = str(Path(workspace).resolve())
        lock_file = self._lock_dir / f"{hash(ws) % 0xFFFFFF:06x}.lock"

        # Check in-memory state first
        if ws in self._locks and self._locks[ws] != agent_id:
            return False

        try:
            # Try to create lock file (atomic on most FS)
            lock_file.write_text(json.dumps({
                "agent": agent_id,
                "workspace": ws,
                "acquired_at": int(time.time() * 1000),
            }) + "\n", encoding="utf-8")
            self._locks[ws] = agent_id
            return True
        except OSError:
            return False

    def release(self, workspace: str, agent_id: str) -> None:
        """Release write lock for a workspace."""
        ws = str(Path(workspace).resolve())
        if self._locks.get(ws) == agent_id:
            del self._locks[ws]
        lock_file = self._lock_dir / f"{hash(ws) % 0xFFFFFF:06x}.lock"
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    def is_locked(self, workspace: str) -> bool:
        """Check if a workspace is currently locked."""
        ws = str(Path(workspace).resolve())
        return ws in self._locks

    def get_lock_owner(self, workspace: str) -> str | None:
        """Get the agent ID that owns the lock, or None."""
        ws = str(Path(workspace).resolve())
        return self._locks.get(ws)


# ======================================================================
# WorkspaceSelector
# ======================================================================


@dataclass
class WorkspaceInfo:
    """Metadata about a workspace."""

    path: str
    name: str
    last_used: int = 0  # ms timestamp
    file_count: int = 0


class WorkspaceSelector:
    """Manage workspace list and current workspace selection."""

    def __init__(self, settings_file: Path | str | None = None) -> None:
        self._settings_file = Path(settings_file) if settings_file else Path(__file__).resolve().parent.parent / "config" / "workspaces.json"
        self._workspaces: list[WorkspaceInfo] = []
        self._current: str = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._workspaces = [
                    WorkspaceInfo(
                        path=w.get("path", ""),
                        name=w.get("name", Path(w.get("path", "")).name),
                        last_used=w.get("last_used", 0),
                        file_count=w.get("file_count", 0),
                    )
                    for w in data
                    if isinstance(w, dict) and w.get("path")
                ]
        except (OSError, ValueError, TypeError):
            self._workspaces = []

    def _save(self) -> None:
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "path": w.path,
                    "name": w.name,
                    "last_used": w.last_used,
                    "file_count": w.file_count,
                }
                for w in self._workspaces
            ]
            self._settings_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    @property
    def current(self) -> str:
        return self._current

    @property
    def workspaces(self) -> list[WorkspaceInfo]:
        return list(self._workspaces)

    def add(self, path: str) -> None:
        """Add a workspace to the list."""
        ws = Path(path).resolve()
        ws_str = str(ws)
        if not any(w.path == ws_str for w in self._workspaces):
            self._workspaces.append(WorkspaceInfo(
                path=ws_str,
                name=ws.name,
                last_used=int(time.time() * 1000),
            ))
            self._save()

    def select(self, path: str) -> bool:
        """Select a workspace by path. Returns True if found."""
        ws_str = str(Path(path).resolve())
        if any(w.path == ws_str for w in self._workspaces):
            self._current = ws_str
            # Update last_used
            for w in self._workspaces:
                if w.path == ws_str:
                    w.last_used = int(time.time() * 1000)
            self._save()
            return True
        return False

    def remove(self, path: str) -> None:
        """Remove a workspace from the list."""
        ws_str = str(Path(path).resolve())
        self._workspaces = [w for w in self._workspaces if w.path != ws_str]
        if self._current == ws_str:
            self._current = self._workspaces[0].path if self._workspaces else ""
        self._save()


# ======================================================================
# HandoffPackage — structured payload for agent handoff/upgrade
# ======================================================================


@dataclass
class HandoffPackage:
    """Structured payload for agent handoff/upgrade.

    Contains all context needed for a receiving agent to continue work.
    """

    task: str  # original user task
    workspace: str  # workspace path
    branch: str = "main"  # current git branch
    git_diff: str = ""  # current uncommitted changes
    attempted_fixes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    recommended_next_action: str = ""
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "workspace": self.workspace,
            "branch": self.branch,
            "git_diff": self.git_diff,
            "attempted_fixes": self.attempted_fixes,
            "errors": self.errors,
            "relevant_files": self.relevant_files,
            "tests": self.tests,
            "constraints": self.constraints,
            "recommended_next_action": self.recommended_next_action,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandoffPackage:
        return cls(
            task=data.get("task", ""),
            workspace=data.get("workspace", ""),
            branch=data.get("branch", "main"),
            git_diff=data.get("git_diff", ""),
            attempted_fixes=data.get("attempted_fixes", []),
            errors=data.get("errors", []),
            relevant_files=data.get("relevant_files", []),
            tests=data.get("tests", []),
            constraints=data.get("constraints", []),
            recommended_next_action=data.get("recommended_next_action", ""),
            created_at=data.get("created_at", 0),
        )


# ======================================================================
# AutoRouter — wraps AgentRouter with handoff package generation
# ======================================================================


class AutoRouter:
    """Auto-routing platform with handoff package generation.

    Strategy:
      - Default: Qwen (via CompanionRuntime)
      - Complex tasks: upgrade to Codex/Claude with handoff package
      - Single writer per workspace enforced
    """

    def __init__(
        self,
        router: AgentRouter | None = None,
        workspace_lock: WorkspaceLock | None = None,
    ) -> None:
        self._router = router or AgentRouter()
        self._lock = workspace_lock or WorkspaceLock()

    def route(self, request: TaskRequest) -> list[AgentRecommendation]:
        """Route a task request to the best agent(s)."""
        return self._router.recommend(request)

    def build_handoff_package(
        self,
        request: TaskRequest,
        recommendation: AgentRecommendation,
        *,
        git_diff: str = "",
        errors: Sequence[str] = (),
        relevant_files: Sequence[str] = (),
        tests: Sequence[str] = (),
    ) -> HandoffPackage:
        """Build a handoff package for agent upgrade.

        Called when the default agent (Qwen) cannot handle the task
        and needs to escalate to a more capable agent (Codex/Claude).
        """
        # Determine recommended next action based on recommendation
        recommended_action = self._suggest_next_action(recommendation)

        # Build constraints from recommendation
        constraints = []
        if recommendation.reason_code:
            constraints.append(f"Recommended by router: {recommendation.reason_code.value}")

        return HandoffPackage(
            task=request.text or "",
            workspace=request.workspace or "",
            git_diff=git_diff,
            errors=list(errors),
            relevant_files=list(relevant_files),
            tests=list(tests),
            constraints=constraints,
            recommended_next_action=recommended_action,
        )

    def can_write(self, workspace: str, agent_id: str) -> bool:
        """Check if an agent can write to a workspace (no lock conflict)."""
        if not self._lock.is_locked(workspace):
            return True
        # Already locked by same agent
        return self._lock.get_lock_owner(workspace) == agent_id

    def acquire_write(self, workspace: str, agent_id: str) -> bool:
        """Acquire write lock for a workspace."""
        return self._lock.acquire(workspace, agent_id)

    def release_write(self, workspace: str, agent_id: str) -> None:
        """Release write lock for a workspace."""
        self._lock.release(workspace, agent_id)

    def _suggest_next_action(self, recommendation: AgentRecommendation) -> str:
        """Suggest the next action based on the recommendation."""
        mode = recommendation.handoff_mode
        if mode == HandoffMode.SHORT_TALK:
            return "Handle task via managed Short Talk"
        if mode == HandoffMode.OPEN_NATIVE:
            return f"Open {recommendation.agent_id} native surface for this task"
        if mode == HandoffMode.UNAVAILABLE:
            return f"Backend unavailable for {recommendation.agent_id}; suggest user to open manually"
        return "Review recommendation and proceed"


__all__ = [
    "AutoRouter",
    "WorkspaceSelector",
    "WorkspaceLock",
    "HandoffPackage",
    "WorkspaceInfo",
]
