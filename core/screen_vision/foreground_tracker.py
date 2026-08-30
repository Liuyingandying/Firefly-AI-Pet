"""Foreground context tracking for capture semantics.

Remembers the most recent NON-Firefly foreground window so a look request
like "看看我在做什么" can capture what the user was doing before the
Companion window took focus — instead of capturing Firefly itself.

Only a lightweight window reference (HWND) is stored: never screenshots,
never window contents, never provider calls. Updates happen on demand at
"about to focus a Firefly window" moments (e.g. the Ask path); there is no
background polling of any kind.

Firefly windows are identified by process id (any top-level window of this
process: companion, pet overlay, bubbles, PageLens, MemoryPanel, ...), not
by window-title strings.
"""

import os
import threading

try:
    import ctypes

    _user32 = ctypes.windll.user32  # Windows only
except (AttributeError, OSError):  # pragma: no cover - non-Windows dev boxes
    _user32 = None


def get_foreground_hwnd() -> int:
    """Current OS foreground window handle (0 when unavailable)."""
    if _user32 is None:
        return 0
    return int(_user32.GetForegroundWindow() or 0)


def window_process_id(hwnd: int) -> int:
    if _user32 is None or not hwnd:
        return 0
    pid = 0
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(ctypes.c_uint32(pid)))
    return int(pid)


def is_firefly_hwnd(hwnd: int) -> bool:
    """True for any top-level window owned by this (Firefly) process."""
    if not hwnd:
        return True  # treat unknown as Firefly so we never record garbage
    return window_process_id(hwnd) == os.getpid()


class ForegroundContextTracker:
    """Remembers the last external (non-Firefly) foreground HWND."""

    def __init__(self, query_foreground=get_foreground_hwnd):
        self._query_foreground = query_foreground
        self._lock = threading.Lock()
        self._last_external_hwnd: int = 0
        self._companion_hwnd: int = 0

    def remember_current_external_window(self) -> int:
        """Record the current foreground window if it is not a Firefly
        window. Call BEFORE focusing a Firefly window (e.g. right at the top
        of the Ask path, while the user's own window is still foreground)."""
        hwnd = int(self._query_foreground() or 0)
        if hwnd and not is_firefly_hwnd(hwnd):
            with self._lock:
                self._last_external_hwnd = hwnd
        return self._last_external_hwnd

    def remember_firefly_window(self, hwnd: int) -> None:
        """Register a Firefly-owned HWND (e.g. the Companion chat window)."""
        if hwnd:
            with self._lock:
                self._companion_hwnd = int(hwnd)

    def forget_firefly_window(self, hwnd: int) -> None:
        with self._lock:
            if self._companion_hwnd == int(hwnd):
                self._companion_hwnd = 0

    @property
    def last_non_firefly_window(self) -> int:
        with self._lock:
            return self._last_external_hwnd

    @property
    def companion_window(self) -> int:
        with self._lock:
            return self._companion_hwnd


foreground_tracker = ForegroundContextTracker()
