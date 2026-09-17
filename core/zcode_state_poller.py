"""Read-only Z Code Harness runtime state adapter (Phase 5C-1).

This module observes Z Code's local SQLite database and converts newly
observed tool/turn rows into the existing provider-neutral ``AgentEvent``
contract.  It deliberately does not own a RuntimeBus lifecycle, does not
read Firefly state files, and performs no network/model work.  A caller may
use :meth:`emit_agent_event` to publish the converted event when it chooses
to attach this adapter to RuntimeBus.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer

from core.agent_events import AgentEvent, AgentEventType
from core.runtime_bus import RuntimeBus, RuntimeEvent

DEFAULT_ZCODE_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
DEFAULT_POLL_INTERVAL_MS = 500

# A turn stops being in-flight when it reaches one of these statuses; only
# then is a FINAL/ERROR/CANCELLED event emitted.
_TERMINAL_TURN_STATUSES = frozenset({"completed", "error", "cancelled"})


@dataclass
class _PollCursor:
    """Incremental position for the two Z Code tables."""

    last_tool_started_at: int = 0
    seen_tool_ids: set[str] = field(default_factory=set)
    last_turn_started_at: int = 0
    seen_turn_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ExternalAgentState:
    """Normalized state observed from one Z Code SQLite row."""

    source: str
    agent_id: str
    session_id: str | None
    turn_id: str | None
    activity: str
    status: str | None
    tool_name: str | None
    tool_id: str | None
    started_at: int | None
    first_token_at: int | None
    completed_at: int | None
    observed_at: int
    error_type: str | None = None
    error_code: str | None = None


class ZCodeStatePoller(QObject):
    """Poll Z Code's SQLite state and convert new rows into AgentEvents.

    The SQLite connection is opened lazily by :meth:`start` or the first
    :meth:`poll_once`, using ``file:<path>?mode=ro`` with ``query_only`` and
    a 100 ms busy timeout.  Existing rows are used only to seed the cursor;
    they are never replayed as events.
    """

    SOURCE = "zcode_harness"
    AGENT_ID = "zcode"

    def __init__(
        self,
        bus: RuntimeBus | None = None,
        db_path: str | Path = DEFAULT_ZCODE_DB,
        *,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        parent: QObject | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._bus = bus
        self.db_path = Path(db_path)
        self.poll_interval_ms = int(poll_interval_ms)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._connection: sqlite3.Connection | None = None
        self._cursor = _PollCursor()
        self._initialized = False
        self._closed = False
        self._last_tool_status: dict[str, str | None] = {}
        self._finished_turn_ids: set[str] = set()
        self._active_turn_ids: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(self.poll_interval_ms)
        self._timer.timeout.connect(self.poll_once)

    @property
    def is_active(self) -> bool:
        return self._timer.isActive()

    @property
    def cursor(self) -> _PollCursor:
        """Expose the live cursor for diagnostics/tests without copying it."""
        return self._cursor

    def start(self) -> None:
        """Seed the cursor, poll once, then start periodic polling."""
        if self._closed:
            return
        if not self._ensure_connection():
            return
        self._initialize_cursor()
        self.poll_once()
        self._timer.start()

    def stop(self) -> None:
        """Stop polling while retaining the read connection and cursor."""
        self._timer.stop()

    def close(self) -> None:
        """Stop polling and close the read-only SQLite connection."""
        if self._closed:
            return
        self._closed = True
        self.stop()
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None

    def poll_once(self) -> list[ExternalAgentState]:
        """Process one incremental snapshot without raising SQLite errors.

        Returns the normalized states represented by events emitted during
        this poll.  A locked, missing, or schema-incompatible database simply
        produces an empty list so the host application remains unaffected.
        """
        if self._closed or not self._ensure_connection():
            return []
        if not self._initialized:
            self._initialize_cursor()

        states: list[ExternalAgentState] = []
        try:
            states.extend(self._poll_tools())
            states.extend(self._poll_turns())
        except sqlite3.Error:
            # Z Code may briefly hold a write lock; retry on the next tick.
            return []
        return states

    def emit_agent_event(self, event: AgentEvent) -> None:
        """Publish one converted event when a RuntimeBus was supplied.

        This is the only RuntimeBus touch point.  The poller never publishes
        state snapshots and never bypasses RuntimeStateAggregator.
        """
        if self._closed or self._bus is None:
            return
        self._bus.publish_event(
            RuntimeEvent(
                kind="agent.event",
                source=self.SOURCE,
                timestamp=event.timestamp,
                payload=event,
            )
        )

    # -- SQLite -----------------------------------------------------------

    def _sqlite_uri(self) -> str:
        # URI paths use forward slashes on Windows; sqlite3 accepts the
        # resulting file:E:/... form with uri=True.
        return f"file:{self.db_path.resolve().as_posix()}?mode=ro"

    def _ensure_connection(self) -> bool:
        if self._connection is not None:
            return True
        try:
            connection = sqlite3.connect(
                self._sqlite_uri(),
                uri=True,
                timeout=0.1,
                isolation_level=None,
            )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=100")
            self._connection = connection
            return True
        except (OSError, sqlite3.Error):
            self._connection = None
            return False

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        if self._connection is None:
            return []
        try:
            self._connection.row_factory = sqlite3.Row
            return list(self._connection.execute(sql, params).fetchall())
        except sqlite3.Error:
            return []

    def _initialize_cursor(self) -> None:
        """Seed at current table heads so historical rows are not replayed."""
        tool_max = self._query("SELECT MAX(started_at) AS max_started FROM tool_usage")
        turn_max = self._query("SELECT MAX(started_at) AS max_started FROM turn_usage")

        tool_started = int(tool_max[0]["max_started"] or 0) if tool_max else 0
        turn_started = int(turn_max[0]["max_started"] or 0) if turn_max else 0
        self._cursor.last_tool_started_at = tool_started
        self._cursor.last_turn_started_at = turn_started

        if tool_started:
            rows = self._query(
                "SELECT id, status, turn_id FROM tool_usage WHERE started_at = ?",
                (tool_started,),
            )
            for row in rows:
                tool_id = str(row["id"])
                self._cursor.seen_tool_ids.add(tool_id)
                self._last_tool_status[tool_id] = row["status"]
                if row["turn_id"]:
                    self._active_turn_ids.add(str(row["turn_id"]))

        if turn_started:
            rows = self._query(
                "SELECT turn_id, status FROM turn_usage WHERE started_at = ?",
                (turn_started,),
            )
            for row in rows:
                turn_id = str(row["turn_id"])
                self._cursor.seen_turn_ids.add(turn_id)
                if row["status"] in _TERMINAL_TURN_STATUSES:
                    self._finished_turn_ids.add(turn_id)

        # Turns that are still running when the poller (re)starts may complete
        # later with a started_at older than the turn cursor.  Watch them by id
        # so their completion still produces a FINAL (restart must not orphan
        # the completion of an in-flight turn).
        rows = self._query(
            "SELECT turn_id, status FROM turn_usage WHERE status = 'running'"
        )
        for row in rows:
            turn_id = str(row["turn_id"])
            self._cursor.seen_turn_ids.add(turn_id)
            self._active_turn_ids.add(turn_id)

        self._initialized = True

    # -- row processing ---------------------------------------------------

    def _poll_tools(self) -> list[ExternalAgentState]:
        rows = self._query(
            """
            SELECT id, session_id, turn_id, tool_name, status, started_at,
                   first_output_at, completed_at, error_type, error_code
            FROM tool_usage
            WHERE started_at >= ?
            ORDER BY started_at ASC, id ASC
            """,
            (self._cursor.last_tool_started_at,),
        )
        output: list[ExternalAgentState] = []
        for row in rows:
            tool_id = str(row["id"])
            started_at = int(row["started_at"] or 0)
            is_new = tool_id not in self._cursor.seen_tool_ids
            previous_status = self._last_tool_status.get(tool_id)
            current_status = row["status"]
            self._cursor.seen_tool_ids.add(tool_id)
            self._last_tool_status[tool_id] = current_status
            self._cursor.last_tool_started_at = max(
                self._cursor.last_tool_started_at,
                started_at,
            )
            turn_id = str(row["turn_id"]) if row["turn_id"] else None
            if turn_id:
                self._active_turn_ids.add(turn_id)

            # New running tool → TOOL.  A running row is emitted once.
            if current_status == "running" and (
                is_new or previous_status != "running"
            ):
                state = self._external_tool_state(row, "tool_running")
                output.append(state)
                self.emit_agent_event(self._to_tool_event(state))
                continue

            # A completed tool while its turn is not terminal means the model
            # is still processing.  Emit STATUS(processing) once per tool.
            if current_status == "completed" and previous_status != "completed":
                if turn_id not in self._finished_turn_ids:
                    state = self._external_tool_state(row, "processing")
                    output.append(state)
                    self.emit_agent_event(self._to_processing_event(state))

        return output

    def _poll_turns(self) -> list[ExternalAgentState]:
        # Query the cursor window (new turns) plus every watched in-flight
        # turn by id, so a turn started before the cursor can still emit its
        # FINAL/ERROR/CANCELLED when it completes after the poller started.
        watched = tuple(sorted(self._active_turn_ids))
        placeholders = ",".join("?" * len(watched))
        sql = """
            SELECT session_id, turn_id, status, started_at,
                   first_token_at, completed_at, error_type, error_code
            FROM turn_usage
            WHERE started_at >= ?
        """
        params: list[object] = [self._cursor.last_turn_started_at]
        if placeholders:
            sql += f" OR turn_id IN ({placeholders})"
            params.extend(watched)
        sql += " ORDER BY started_at ASC, turn_id ASC"
        rows = self._query(sql, tuple(params))

        output: list[ExternalAgentState] = []
        for row in rows:
            turn_id = str(row["turn_id"])
            started_at = int(row["started_at"] or 0)
            is_new = turn_id not in self._cursor.seen_turn_ids
            is_watched = turn_id in self._active_turn_ids
            self._cursor.seen_turn_ids.add(turn_id)
            self._cursor.last_turn_started_at = max(
                self._cursor.last_turn_started_at,
                started_at,
            )
            status = row["status"]
            if turn_id in self._finished_turn_ids:
                continue
            if not is_new and not is_watched:
                continue
            if status not in _TERMINAL_TURN_STATUSES:
                # Still in flight; keep watching until it completes.
                continue
            self._finished_turn_ids.add(turn_id)
            self._active_turn_ids.discard(turn_id)
            state = self._external_turn_state(row)
            output.append(state)
            self.emit_agent_event(self._to_turn_event(state))
        return output

    def _external_tool_state(self, row: sqlite3.Row, activity: str) -> ExternalAgentState:
        return ExternalAgentState(
            source=self.SOURCE,
            agent_id=self.AGENT_ID,
            session_id=str(row["session_id"]) if row["session_id"] else None,
            turn_id=str(row["turn_id"]) if row["turn_id"] else None,
            activity=activity,
            status=row["status"],
            tool_name=row["tool_name"],
            tool_id=str(row["id"]) if row["id"] else None,
            started_at=int(row["started_at"]) if row["started_at"] else None,
            first_token_at=None,
            completed_at=int(row["completed_at"]) if row["completed_at"] else None,
            observed_at=self._clock(),
            error_type=row["error_type"],
            error_code=row["error_code"],
        )

    def _external_turn_state(self, row: sqlite3.Row) -> ExternalAgentState:
        status = row["status"]
        activity = {
            "completed": "success",
            "error": "error",
            "cancelled": "cancelled",
        }.get(status, status or "unknown")
        return ExternalAgentState(
            source=self.SOURCE,
            agent_id=self.AGENT_ID,
            session_id=str(row["session_id"]) if row["session_id"] else None,
            turn_id=str(row["turn_id"]) if row["turn_id"] else None,
            activity=activity,
            status=status,
            tool_name=None,
            tool_id=None,
            started_at=int(row["started_at"]) if row["started_at"] else None,
            first_token_at=(
                int(row["first_token_at"]) if row["first_token_at"] else None
            ),
            completed_at=(
                int(row["completed_at"]) if row["completed_at"] else None
            ),
            observed_at=self._clock(),
            error_type=row["error_type"],
            error_code=row["error_code"],
        )

    # -- AgentEvent conversion -------------------------------------------

    @staticmethod
    def _event_timestamp(state: ExternalAgentState) -> int:
        return state.completed_at or state.started_at or state.observed_at

    def _to_tool_event(self, state: ExternalAgentState) -> AgentEvent:
        return AgentEvent(
            agent_id=state.agent_id,
            type=AgentEventType.TOOL,
            timestamp=self._event_timestamp(state),
            session_id=state.session_id,
            tool_name=state.tool_name,
            status="running_tool",
        )

    def _to_processing_event(self, state: ExternalAgentState) -> AgentEvent:
        return AgentEvent(
            agent_id=state.agent_id,
            type=AgentEventType.STATUS,
            timestamp=self._event_timestamp(state),
            session_id=state.session_id,
            status="processing",
        )

    def _to_turn_event(self, state: ExternalAgentState) -> AgentEvent:
        event_type = {
            "completed": AgentEventType.FINAL,
            "error": AgentEventType.ERROR,
            "cancelled": AgentEventType.CANCELLED,
        }.get(state.status, AgentEventType.ERROR)
        return AgentEvent(
            agent_id=state.agent_id,
            type=event_type,
            timestamp=self._event_timestamp(state),
            session_id=state.session_id,
            error_code=state.error_code or state.error_type,
        )
