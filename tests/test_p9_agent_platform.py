"""Phase v0.3-P9: Agent Front Platform tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.agent_platform import (
    AutoRouter,
    HandoffPackage,
    WorkspaceInfo,
    WorkspaceLock,
    WorkspaceSelector,
)
from core.routing_models import (
    HandoffMode,
    TaskIntent,
    TaskRequest,
)


# ======================================================================
# P9-A: WorkspaceLock — single writer per workspace
# ======================================================================


class TestP9A_WorkspaceLock:
    """Single writer agent per workspace enforcement."""

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        lock = WorkspaceLock(tmp_path / "locks")
        ws = str(tmp_path / "workspace")

        assert lock.acquire(ws, "agent1") is True
        assert lock.is_locked(ws) is True
        assert lock.get_lock_owner(ws) == "agent1"

        lock.release(ws, "agent1")
        assert lock.is_locked(ws) is False
        assert lock.get_lock_owner(ws) is None

    def test_conflict_prevents_double_write(self, tmp_path: Path) -> None:
        lock = WorkspaceLock(tmp_path / "locks")
        ws = str(tmp_path / "workspace")

        lock.acquire(ws, "agent1")
        # Second agent should fail
        assert lock.acquire(ws, "agent2") is False
        assert lock.get_lock_owner(ws) == "agent1"

    def test_same_agent_can_reacquire(self, tmp_path: Path) -> None:
        lock = WorkspaceLock(tmp_path / "locks")
        ws = str(tmp_path / "workspace")

        lock.acquire(ws, "agent1")
        # Same agent can "reacquire" (no-op success)
        assert lock.acquire(ws, "agent1") is True

    def test_release_by_wrong_agent_noop(self, tmp_path: Path) -> None:
        lock = WorkspaceLock(tmp_path / "locks")
        ws = str(tmp_path / "workspace")

        lock.acquire(ws, "agent1")
        lock.release(ws, "agent2")  # wrong agent
        assert lock.is_locked(ws) is True
        assert lock.get_lock_owner(ws) == "agent1"


# ======================================================================
# P9-B: WorkspaceSelector
# ======================================================================


class TestP9B_WorkspaceSelector:
    """Workspace list management."""

    def test_empty_initial_state(self, tmp_path: Path) -> None:
        sel = WorkspaceSelector(tmp_path / "ws.json")
        assert sel.current == ""
        assert sel.workspaces == []

    def test_add_and_select(self, tmp_path: Path) -> None:
        sel = WorkspaceSelector(tmp_path / "ws.json")
        ws = tmp_path / "myproject"
        ws.mkdir()

        sel.add(str(ws))
        assert sel.select(str(ws)) is True
        assert sel.current == str(ws)
        assert len(sel.workspaces) == 1
        assert sel.workspaces[0].name == "myproject"

    def test_remove(self, tmp_path: Path) -> None:
        sel = WorkspaceSelector(tmp_path / "ws.json")
        ws1 = tmp_path / "project1"
        ws2 = tmp_path / "project2"
        ws1.mkdir()
        ws2.mkdir()

        sel.add(str(ws1))
        sel.add(str(ws2))
        sel.select(str(ws1))

        sel.remove(str(ws1))
        assert sel.current == str(ws2)  # falls back to first
        assert len(sel.workspaces) == 1

    def test_duplicate_add_ignored(self, tmp_path: Path) -> None:
        sel = WorkspaceSelector(tmp_path / "ws.json")
        ws = tmp_path / "project"
        ws.mkdir()

        sel.add(str(ws))
        sel.add(str(ws))  # duplicate
        assert len(sel.workspaces) == 1

    def test_select_nonexistent_returns_false(self, tmp_path: Path) -> None:
        sel = WorkspaceSelector(tmp_path / "ws.json")
        assert sel.select("/nonexistent/path") is False


# ======================================================================
# P9-C: HandoffPackage
# ======================================================================


class TestP9C_HandoffPackage:
    """Handoff package serialization and round-trip."""

    def test_create_package(self) -> None:
        pkg = HandoffPackage(
            task="Fix the login bug",
            workspace="/home/user/project",
            git_diff="diff --git a/main.py ...",
            errors=["TypeError: ..."],
            relevant_files=["main.py", "auth.py"],
            recommended_next_action="Review auth.py changes",
        )
        assert pkg.task == "Fix the login bug"
        assert len(pkg.errors) == 1
        assert len(pkg.relevant_files) == 2

    def test_round_trip(self) -> None:
        original = HandoffPackage(
            task="Refactor database layer",
            workspace="/home/user/db-project",
            branch="feature/db-refactor",
            git_diff="diff --git a/db.py ...",
            attempted_fixes=["Try connection pooling"],
            errors=["ConnectionError: timeout"],
            relevant_files=["db.py", "models.py"],
            tests=["test_db.py", "test_models.py"],
            constraints=["Must support PostgreSQL"],
            recommended_next_action="Test with PostgreSQL",
        )
        restored = HandoffPackage.from_dict(original.to_dict())
        assert restored.task == original.task
        assert restored.workspace == original.workspace
        assert restored.branch == original.branch
        assert restored.git_diff == original.git_diff
        assert restored.attempted_fixes == original.attempted_fixes
        assert restored.errors == original.errors
        assert restored.relevant_files == original.relevant_files
        assert restored.tests == original.tests
        assert restored.constraints == original.constraints
        assert restored.recommended_next_action == original.recommended_next_action

    def test_default_values(self) -> None:
        pkg = HandoffPackage(task="test", workspace="/ws")
        assert pkg.branch == "main"
        assert pkg.git_diff == ""
        assert pkg.attempted_fixes == []
        assert pkg.errors == []
        assert pkg.relevant_files == []
        assert pkg.tests == []
        assert pkg.constraints == []
        assert pkg.recommended_next_action == ""


# ======================================================================
# P9-D: AutoRouter
# ======================================================================


class TestP9D_AutoRouter:
    """Auto-routing with handoff package generation."""

    def test_route_delegates_to_agent_router(self) -> None:
        mock_router = MagicMock()
        mock_router.recommend.return_value = []
        auto = AutoRouter(router=mock_router)

        request = TaskRequest(text="write code", workspace="/ws")
        result = auto.route(request)

        mock_router.recommend.assert_called_once_with(request)

    def test_build_handoff_package(self) -> None:
        auto = AutoRouter()
        recommendation = MagicMock()
        recommendation.handoff_mode = HandoffMode.SHORT_TALK
        recommendation.reason_code = MagicMock()
        recommendation.reason_code.value = "best_for_coding"
        recommendation.agent_id = "codex"

        request = TaskRequest(
            text="Fix the memory leak",
            workspace="/home/user/project",
        )

        pkg = auto.build_handoff_package(
            request,
            recommendation,
            git_diff="diff --git a/leak.py ...",
            errors=["MemoryError: ..."],
            relevant_files=["leak.py"],
            tests=["test_leak.py"],
        )

        assert pkg.task == "Fix the memory leak"
        assert pkg.workspace == "/home/user/project"
        assert pkg.git_diff == "diff --git a/leak.py ..."
        assert "MemoryError" in pkg.errors[0]
        assert "leak.py" in pkg.relevant_files
        assert "test_leak.py" in pkg.tests
        # Constraints come from the recommendation reason code, not from request
        assert len(pkg.constraints) >= 1

    def test_can_write_no_lock(self) -> None:
        auto = AutoRouter()
        assert auto.can_write("/ws", "agent1") is True

    def test_can_write_locked_by_other(self, tmp_path: Path) -> None:
        lock = WorkspaceLock(tmp_path / "locks")
        auto = AutoRouter(workspace_lock=lock)
        ws = str(tmp_path / "ws")

        lock.acquire(ws, "agent1")
        assert auto.can_write(ws, "agent2") is False

    def test_can_write_locked_by_same(self, tmp_path: Path) -> None:
        lock = WorkspaceLock(tmp_path / "locks")
        auto = AutoRouter(workspace_lock=lock)
        ws = str(tmp_path / "ws")

        lock.acquire(ws, "agent1")
        assert auto.can_write(ws, "agent1") is True

    def test_acquire_and_release_write(self, tmp_path: Path) -> None:
        lock = WorkspaceLock(tmp_path / "locks")
        auto = AutoRouter(workspace_lock=lock)
        ws = str(tmp_path / "ws")

        assert auto.acquire_write(ws, "agent1") is True
        assert auto.can_write(ws, "agent1") is True

        auto.release_write(ws, "agent1")
        assert auto.can_write(ws, "agent1") is True
