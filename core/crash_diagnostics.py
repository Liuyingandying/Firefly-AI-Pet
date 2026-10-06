"""Local crash evidence for the desktop process; never changes app behavior."""

from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

_stream: TextIO | None = None
_lock = threading.RLock()
_thread_timer = None
_qt_handler = None


def _write(line: str) -> None:
    if _stream is None:
        return
    with _lock:
        _stream.write(f"{datetime.now(timezone.utc).isoformat()} {line}\n")
        _stream.flush()


def log_diagnostic(event: str, **fields: object) -> None:
    """Record bounded metadata, with no student answer or provider payload."""
    details = " ".join(f"{key}={str(value)[:240]!r}" for key, value in fields.items())
    _write(f"[CRASH_DIAG] event={event} {details}".rstrip())


def log_thread(event: str) -> None:
    thread = threading.current_thread()
    log_diagnostic(
        event,
        thread_name=thread.name,
        ident=thread.ident,
        native_id=threading.get_native_id(),
    )


def log_thread_snapshot() -> None:
    threads = [
        f"{thread.name}:{thread.ident}:{thread.native_id}"
        for thread in threading.enumerate()
    ]
    log_diagnostic("threads", entries=",".join(threads))


def check_qobject_thread(label: str, obj) -> None:
    """Log a GUI delivery boundary without changing its control flow."""
    from PySide6.QtCore import QThread

    if QThread.currentThread() != obj.thread():
        log_thread(f"qt_thread_violation_{label}")


def install_crash_diagnostics(logs_dir: Path) -> Path:
    """Install Python, faulthandler and Qt logs before constructing the shell."""
    global _stream, _qt_handler
    if _stream is not None:
        return Path(_stream.name)
    crash_dir = logs_dir / "crash"
    crash_dir.mkdir(parents=True, exist_ok=True)
    path = crash_dir / f"app_{os.getpid()}_{datetime.now():%Y%m%d_%H%M%S}.log"
    _stream = path.open("a", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_stream, all_threads=True)

    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def sys_hook(exc_type, exc_value, exc_traceback) -> None:
        log_thread("python_unhandled")
        with _lock:
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=_stream)
            _stream.flush()
        previous_sys_hook(exc_type, exc_value, exc_traceback)

    def thread_hook(args) -> None:
        log_diagnostic(
            "python_thread_unhandled",
            thread_name=args.thread.name if args.thread else "unknown",
            ident=args.thread.ident if args.thread else None,
            native_id=args.thread.native_id if args.thread else None,
        )
        with _lock:
            traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=_stream)
            _stream.flush()
        previous_thread_hook(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook

    from PySide6.QtCore import qInstallMessageHandler, qVersion

    def qt_handler(kind, context, message) -> None:
        log_diagnostic(
            "qt_message",
            kind=getattr(kind, "name", str(kind)),
            category=getattr(context, "category", ""),
            file=getattr(context, "file", ""),
            line=getattr(context, "line", 0),
            message=str(message)[:600],
        )

    _qt_handler = qt_handler  # keep the Python callback alive for Qt
    qInstallMessageHandler(_qt_handler)
    log_diagnostic("start", pid=os.getpid(), executable=sys.executable,
                   python=sys.version.split()[0], qt=qVersion())
    log_thread("main_thread")
    log_thread_snapshot()
    atexit.register(lambda: log_diagnostic("python_atexit", pid=os.getpid()))
    return path


def watch_startup(timeout_s: int = 15) -> None:
    """Dump all threads if the original GUI startup gets stuck."""
    if _stream is not None:
        faulthandler.dump_traceback_later(timeout_s, repeat=False, file=_stream)
        log_diagnostic("startup_watch_started", timeout_s=timeout_s)


def startup_ready(app) -> None:
    """Mark Qt startup complete and sample thread identities periodically."""
    global _thread_timer
    faulthandler.cancel_dump_traceback_later()
    from PySide6.QtCore import QTimer

    log_diagnostic("startup_ready", pid=os.getpid())
    log_thread_snapshot()
    _thread_timer = QTimer(app)
    _thread_timer.setInterval(30000)
    _thread_timer.timeout.connect(log_thread_snapshot)
    _thread_timer.start()
