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
    pid = ctypes.c_uint32(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_process_name(hwnd: int) -> str:
    """Image file name (lowercase basename) of the window's process, "" when
    unknown."""
    if _user32 is None or not hwnd:
        return ""
    pid = window_process_id(hwnd)
    if not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(512)
        size = ctypes.c_uint32(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return (buffer.value or "").rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_window_hwnd(
    title_needles: list[str],
    processes: tuple[str, ...] = ("msedge.exe", "chrome.exe"),
) -> int:
    """Top-level visible window whose title contains any needle and whose
    process is one of ``processes`` (empty tuple = any process).

    Deterministic window resolution for the PDF OCR Overlay entry: the
    caller derives needles from the open PDF's title/file name, so a
    maximized browser whose ACTIVE tab is the paper matches while an
    unrelated foreground window does not. Returns 0 when nothing matches.
    """
    if _user32 is None:
        return 0
    needles = [str(n).lower() for n in (title_needles or []) if n]
    if not needles:
        return 0
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
            return True
        buffer = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, buffer, 512)
        title = (buffer.value or "").lower()
        if not title or not any(needle in title for needle in needles):
            return True
        if processes and window_process_name(hwnd) not in processes:
            return True
        found.append(int(hwnd) if hwnd else 0)
        return False

    _user32.EnumWindows(callback, None)
    return found[0] if found else 0


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
