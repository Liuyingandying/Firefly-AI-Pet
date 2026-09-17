"""Visual state debounce for the LED hardware output (Phase 4.5 / 4.6).

Sits between ``PetStateResolver.pet_detail_state_changed`` and
``app._write_led_state`` so the LED strip only shows state changes a human
can actually perceive.

Phase 4.5 — entry gate
    SUCCESS / ERROR / WAITING_INPUT: always forwarded immediately.
    WORKING / IDLE: forwarded immediately (low priority but never held).
    TOOL_RUNNING: must persist >= ``TOOL_RUNNING_MIN_MS`` (800 ms) before the
    LEDs switch to it; a shorter blip is dropped entirely.

Phase 4.6 — minimum visible time (hysteresis)
    Once TOOL_RUNNING has been accepted and shown, it stays visible for at
    least ``TOOL_RUNNING_MIN_VISIBLE_MS`` (700 ms) counted from the moment it
    was actually output.  Low-priority fallbacks (WORKING / IDLE) arriving
    inside that window are deferred (last one wins) instead of yanking the
    effect away; high-priority states (WAITING_INPUT / SUCCESS / ERROR)
    preempt the window instantly.  A renewed TOOL_RUNNING inside the window
    clears the deferred fallback (no flash back to WORKING).

Two timers, two responsibilities: the entry-gate timer decides *whether*
TOOL_RUNNING becomes visible; the minimum-visible timer decides how long it
stays visible once it is.

The filter is a pure visual layer: the Runtime resolver remains the single
business truth, ``led_state.json`` format is unchanged, and neither the LED
bridge nor the ESP32 firmware is touched.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from core.user_paths import get_user_data_paths

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger("firefly.visual_filter")

# TOOL_RUNNING must hold this long before the LEDs switch to it (4.5).
TOOL_RUNNING_MIN_MS = 800

# Once shown, TOOL_RUNNING stays visible at least this long (4.6).
TOOL_RUNNING_MIN_VISIBLE_MS = 700

# States that instantly preempt a pending candidate or a running
# minimum-visible window: the user must see them without any delay.
_PREEMPT_STATES = frozenset({"waiting_input", "success", "error"})

LOG_DIR = get_user_data_paths().logs / "visual_filter"
LOG_FILE = LOG_DIR / "visual_filter.log"
_MAX_LOG_BYTES = 512 * 1024


def _attach_file_log() -> None:
    """Best-effort operational log (the pet runs windowless: no stderr).

    Mirrors the bridge convention (each peripheral owns its small log).
    Silent on any OSError and never raises.
    """
    if log.handlers:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size >= _MAX_LOG_BYTES:
            LOG_FILE.write_text("", encoding="utf-8")
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    except OSError:
        pass


_attach_file_log()


class VisualStateFilter(QObject):
    """Debounce + minimum-visible-time layer for the six-state LED stream."""

    state_filtered = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        tool_running_min_ms: int = TOOL_RUNNING_MIN_MS,
        tool_running_min_visible_ms: int = TOOL_RUNNING_MIN_VISIBLE_MS,
    ) -> None:
        super().__init__(parent)
        self._min_ms = int(tool_running_min_ms)
        self._min_visible_ms = int(tool_running_min_visible_ms)
        self._displayed: str | None = None        # last state sent to LED
        self._candidate: str | None = None        # TOOL_RUNNING awaiting gate
        self._candidate_since: float | None = None
        self._visible_active = False              # minimum-visible window
        self._visible_since: float | None = None
        self._pending_fallback: str | None = None  # deferred WORKING / IDLE

        # Entry gate timer (4.5): may TOOL_RUNNING become visible?
        self._entry_timer = QTimer(self)
        self._entry_timer.setSingleShot(True)
        self._entry_timer.timeout.connect(self._accept_candidate)

        # Minimum-visible timer (4.6): how long must it stay visible?
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.timeout.connect(self._on_visible_elapsed)

    # -- public feed (wired to resolver.pet_detail_state_changed) ----------

    def offer(self, state: str) -> None:
        """Offer one resolved detail state to the LED path."""
        # 1. High-priority states always preempt, instantly.
        if state in _PREEMPT_STATES:
            self._cancel_candidate()
            self._cancel_visible(f"preempted by {state}")
            self._pending_fallback = None
            if state != self._displayed:
                self._emit(state)
            return

        # 2. TOOL_RUNNING: entry gate, then minimum-visible window.
        if state == "tool_running":
            if self._visible_active:
                # Still inside the minimum-visible window: keep showing
                # TOOL_RUNNING and drop any deferred fallback (no flash back).
                if self._pending_fallback is not None:
                    log.info(
                        "VISUAL_FILTER: minimum-visible fallback cleared by "
                        "renewed TOOL_RUNNING"
                    )
                    self._pending_fallback = None
                return
            if self._candidate is not None:
                return  # entry window already running
            if self._displayed == "tool_running":
                return  # already on the LEDs (dedup)
            self._candidate = "tool_running"
            self._candidate_since = time.monotonic()
            self._entry_timer.start(self._min_ms)
            return

        # 3. Low-priority states (WORKING / IDLE / anything else).
        self._cancel_candidate()  # a blip never existed for the LEDs

        if self._visible_active:
            # TOOL_RUNNING must stay visible: defer, last one wins.
            self._pending_fallback = state
            remaining = self._visible_remaining_ms()
            log.info(
                "VISUAL_FILTER: fallback deferred state=%s remaining=%dms",
                state, remaining,
            )
            return

        if state != self._displayed:
            self._emit(state)

    # -- entry gate (4.5) ---------------------------------------------------

    def _cancel_candidate(self) -> None:
        if self._candidate is None:
            return
        duration_ms = int((time.monotonic() - self._candidate_since) * 1000)
        self._candidate = None
        self._candidate_since = None
        self._entry_timer.stop()
        log.info("VISUAL_FILTER: TOOL_RUNNING suppressed duration=%dms", duration_ms)

    def _accept_candidate(self) -> None:
        if self._candidate != "tool_running":
            return
        duration_ms = int((time.monotonic() - self._candidate_since) * 1000)
        self._candidate = None
        self._candidate_since = None
        log.info("VISUAL_FILTER: TOOL_RUNNING accepted duration=%dms", duration_ms)
        self._emit("tool_running")
        self._start_visible()

    # -- minimum visible time (4.6) ----------------------------------------

    def _start_visible(self) -> None:
        self._visible_active = True
        self._visible_since = time.monotonic()
        self._pending_fallback = None
        self._visible_timer.start(self._min_visible_ms)
        log.info(
            "VISUAL_FILTER: TOOL_RUNNING minimum-visible started duration=%dms",
            self._min_visible_ms,
        )

    def _visible_remaining_ms(self) -> int:
        if not self._visible_active or self._visible_since is None:
            return 0
        elapsed_ms = (time.monotonic() - self._visible_since) * 1000
        return max(0, int(self._min_visible_ms - elapsed_ms))

    def _cancel_visible(self, reason: str) -> None:
        if not self._visible_active:
            return
        self._visible_active = False
        self._visible_since = None
        self._visible_timer.stop()
        log.info("VISUAL_FILTER: minimum-visible %s", reason)

    def _on_visible_elapsed(self) -> None:
        self._visible_active = False
        self._visible_since = None
        fallback = self._pending_fallback
        self._pending_fallback = None
        if fallback is None:
            log.info("VISUAL_FILTER: minimum-visible elapsed (no fallback)")
            return
        log.info("VISUAL_FILTER: minimum-visible elapsed -> %s", fallback)
        self._emit(fallback)

    # -- emit ---------------------------------------------------------------

    def _emit(self, state: str) -> None:
        self._displayed = state
        self.state_filtered.emit(state)
