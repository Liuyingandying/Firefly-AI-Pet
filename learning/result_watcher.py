"""Firefly-side result watcher (Return Channel v0.1, Phase 6).

Non-blocking QTimer polling of ``<course>/bridge/result.json`` after a
headless learning task starts. On a VALID result whose task_id matches the
watched run: emit it once and stop. On timeout: emit a single gentle
"still working" hint (never a failure verdict) and keep watching at a slower
cadence. Malformed results are surfaced as structured errors — never
silently ignored, never parsed out of launch.log.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from learning.diagnostics import log_marker
from learning._storage import BridgeFileError
from learning.learning_result import read_result, result_path


class LearningResultWatcher(QObject):
    result_received = Signal(dict)   # validated result for the watched task
    result_invalid = Signal(str)     # structured error (schema/parse failure)
    still_working = Signal(str)      # one-shot timeout hint (task_id)
    stopped = Signal()

    def __init__(
        self,
        course_dir: Path | str,
        task_id: str,
        *,
        parent: QObject | None = None,
        poll_ms: int = 400,
        slow_poll_ms: int = 2000,
        timeout_s: float = 900.0,
    ):
        super().__init__(parent)
        self._course_dir = Path(course_dir)
        self._task_id = task_id
        self._poll_ms = poll_ms
        self._slow_poll_ms = slow_poll_ms
        self._timeout_s = timeout_s
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._elapsed_ms = 0
        self._hinted = False
        self._done = False

    def start(self) -> None:
        log_marker("LEARNING_RESULT_WAIT", task_id=self._task_id)
        self._timer.start(self._poll_ms)

    def stop(self) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.stopped.emit()

    def _tick(self) -> None:
        if self._done:
            return
        self._elapsed_ms += self._timer.interval()
        path = result_path(self._course_dir)
        if not path.is_file():
            self._on_tick_timeout()
            return
        try:
            data = read_result(self._course_dir)
        except BridgeFileError as exc:
            # Structured but invalid / malformed: fail loudly, stop watching.
            self._done = True
            self._timer.stop()
            log_marker("LEARNING_RESULT_RECEIVED", task_id=self._task_id, error=exc.code)
            self.result_invalid.emit(f"[{exc.code}] {exc.message}")
            self.stopped.emit()
            return
        if str(data.get("task_id", "")) != self._task_id:
            # stale result from a previous run — refuse and keep waiting
            self._on_tick_timeout()
            return
        self._done = True
        self._timer.stop()
        log_marker(
            "LEARNING_RESULT_RECEIVED", task_id=self._task_id, type=data["message_type"]
        )
        self.result_received.emit(data)
        self.stopped.emit()

    def _on_tick_timeout(self) -> None:
        if not self._hinted and self._elapsed_ms >= self._timeout_s * 1000:
            self._hinted = True
            log_marker("LEARNING_RESULT_WAIT", task_id=self._task_id, phase="timeout-hint")
            self.still_working.emit(self._task_id)
            # never declare failure: keep watching, just cheaper
            self._timer.start(self._slow_poll_ms)
