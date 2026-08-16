"""Single-instance regression tests for the Firefly AI Pet.

Background
----------
`start_pet.ps1` launches `.venv\\Scripts\\pythonw.exe app.py`. On Windows that
executable is a *venv launcher stub*: it re-execs the base interpreter
(`E:\\conda\\pythonw.exe`) and stays alive as its parent for the app's whole
lifetime. Both the stub and the real interpreter carry the SAME command line
(`... pythonw.exe "E:\\Firefly_AI_Pet\\app.py"`), so counting processes by
name + command line reports two "instances" for a single running pet.

Layer 1 -- unit tests (safe, no live pet, test-specific mutex names):
  named-mutex first/second/release/reacquire, two-process atomicity,
  PID-is-not-a-lock, QLocalServer stale recovery.

Layer 2 -- Windows integration (opt-in with --integration; starts and stops the
  live pet): normal start, repeated start, direct `python app.py` /
  `pythonw app.py` second launch, stop, restart, abnormal kill, stop-idempotent.
  Instance counting excludes the venv launcher stub so it measures REAL app
  instances only.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ctypes
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

VENV_SCRIPTS = str((PROJECT_DIR / ".venv" / "Scripts").resolve()).lower()
APP_PATH = PROJECT_DIR / "app.py"
START_PS1 = PROJECT_DIR / "start_pet.ps1"
STOP_PS1 = PROJECT_DIR / "stop_pet.ps1"
VENV_PYTHON = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
VENV_PYTHONW = PROJECT_DIR / ".venv" / "Scripts" / "pythonw.exe"

ERROR_ALREADY_EXISTS = 183
TEST_MUTEX = "FireflyAIPet-TestMutex-8B4"


# -- mutex helpers (mirror app.py exactly, but return the handle) ---------


def _try_mutex(name: str) -> tuple[bool, int | None]:
    """Mirror app._acquire_single_instance; return (acquired, handle)."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False, None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False, None
    return True, handle


def _close(handle: int) -> None:
    ctypes.windll.kernel32.CloseHandle(handle)


# -- accurate instance counting ------------------------------------------


def app_instances() -> list[int]:
    """Return PIDs of REAL Firefly app instances (excludes the venv stub).

    Matches the actual app launch path (Firefly_AI_Pet\\app.py) rather than the
    loose substring "app.py", so helper processes that merely mention the file
    in their own command line are never counted.
    """
    script = (
        "$q = 'Firefly_AI_Pet[\\\\/]app\\.py'; "
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -match $q } | "
        "ForEach-Object { '{0}|{1}' -f $_.ProcessId, $_.ExecutablePath }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        pid, exe = line.split("|", 1)
        exe_low = str(Path(exe).resolve()).lower() if exe else ""
        if exe_low.startswith(VENV_SCRIPTS):
            continue  # venv launcher stub, not a real app instance
        pids.append(int(pid))
    return pids


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _wait_for_count(target: int, timeout_ms: int = 20_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if len(app_instances()) == target:
            return True
        time.sleep(0.2)
    return len(app_instances()) == target


# -- Layer 1: unit tests --------------------------------------------------


def test_mutex_first_acquire() -> None:
    acquired, handle = _try_mutex(TEST_MUTEX)
    try:
        assert acquired, "first CreateMutexW should succeed"
        assert handle, "first CreateMutexW should return a handle"
    finally:
        if handle:
            _close(handle)


def test_mutex_second_acquire_fails() -> None:
    acquired, handle = _try_mutex(TEST_MUTEX)
    assert acquired and handle
    try:
        second, second_handle = _try_mutex(TEST_MUTEX)
        assert not second, "second acquire must report already-exists"
        assert second_handle is None
    finally:
        _close(handle)


def test_mutex_release_then_reacquire() -> None:
    acquired, handle = _try_mutex(TEST_MUTEX)
    assert acquired and handle
    _close(handle)  # release
    reacquired, rehandle = _try_mutex(TEST_MUTEX)
    try:
        assert reacquired, "after release a fresh acquire must succeed"
    finally:
        if rehandle:
            _close(rehandle)


def test_mutex_two_processes_atomic() -> None:
    # The ACQUIRED child HOLDS the mutex (like the real app, which keeps its
    # handle for the whole process lifetime) so the sibling's check is
    # deterministic; a child that releases immediately would let a late start
    # re-create the object and spuriously report two owners.
    child = "\n".join(
        [
            "import ctypes, time",
            "k = ctypes.windll.kernel32",
            f"h = k.CreateMutexW(None, False, {TEST_MUTEX!r})",
            "if not h:",
            "    print('FAILED')",
            "elif k.GetLastError() == 183:",
            "    k.CloseHandle(h)",
            "    print('EXISTS')",
            "else:",
            "    print('ACQUIRED')",
            "    time.sleep(3)",
            "    k.CloseHandle(h)",
        ]
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.PIPE),
        subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.PIPE),
    ]
    out = [p.communicate(timeout=30)[0].decode().strip() for p in procs]
    assert out.count("ACQUIRED") == 1, f"expected exactly one owner, got {out}"
    assert out.count("EXISTS") == 1, f"expected exactly one already-exists, got {out}"


def test_pid_not_a_lock() -> None:
    source = APP_PATH.read_text(encoding="utf-8")
    assert "PID_FILE" in source  # sanity: the constant still exists
    # app.py must never treat pet.pid as a lock (no read/exists/stat gate).
    assert "PID_FILE.exists()" not in source
    assert "PID_FILE.read_text" not in source
    assert "PID_FILE.stat" not in source
    stop_source = (PROJECT_DIR / "tools" / "stop_pet.py").read_text(encoding="utf-8")
    # only stop_pet.py reads the pid file, and solely to remove a stale one.
    assert "PID_FILE.exists()" in stop_source


def test_qlocal_server_stale_recovery() -> None:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalServer

    app = QCoreApplication.instance() or QCoreApplication([])
    name = "FireflyAIPet-TestServer-8B4"
    server = QLocalServer()
    try:
        server.removeServer(name)  # clear any stale artifact
        assert server.listen(name), "first listen must succeed"
        server.close()
        server.removeServer(name)  # simulate a stale leftover
        server2 = QLocalServer()
        assert server2.listen(name), "re-listen after stale cleanup must succeed"
        server2.close()
    finally:
        server.removeServer(name)


def run_unit() -> None:
    test_mutex_first_acquire()
    test_mutex_second_acquire_fails()
    test_mutex_release_then_reacquire()
    test_mutex_two_processes_atomic()
    test_pid_not_a_lock()
    test_qlocal_server_stale_recovery()
    print("Single-instance unit tests passed.")


# -- Layer 2: Windows integration (live pet) ------------------------------

INTEGRATION_SKIP = os.name != "nt"


def test_integration_start_single() -> None:
    _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
          "-File", str(START_PS1)])
    assert _wait_for_count(1), f"start must yield 1 real instance, got {app_instances()}"


def test_integration_repeated_start_single() -> None:
    for _ in range(5):
        _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
              "-File", str(START_PS1)])
        time.sleep(0.4)
    assert _wait_for_count(1), f"repeated start must stay 1, got {app_instances()}"


def test_integration_direct_second_launches() -> None:
    # direct `python app.py` while the pet runs must not create a second instance
    _run([str(VENV_PYTHON), str(APP_PATH)], timeout=60)
    time.sleep(1.5)
    assert len(app_instances()) == 1, f"python app.py must not add an instance, got {app_instances()}"
    # direct `pythonw app.py` likewise
    _run([str(VENV_PYTHONW), str(APP_PATH)], timeout=60)
    time.sleep(1.5)
    assert len(app_instances()) == 1, f"pythonw app.py must not add an instance, got {app_instances()}"


def test_integration_stop_zero() -> None:
    _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
          "-File", str(STOP_PS1)])
    assert _wait_for_count(0), f"stop must leave 0 instances, got {app_instances()}"


def test_integration_restart_single() -> None:
    _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
          "-File", str(START_PS1)])
    assert _wait_for_count(1), f"restart must yield 1 instance, got {app_instances()}"


def test_integration_abnormal_kill_restart() -> None:
    pids = app_instances()
    assert len(pids) == 1
    pid = pids[0]
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
          f"Stop-Process -Id {pid} -Force -Confirm:$false"])
    assert _wait_for_count(0), f"kill must leave 0 instances (stub should exit too), got {app_instances()}"
    # restart must be clean
    _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
          "-File", str(START_PS1)])
    assert _wait_for_count(1), f"restart after kill must yield 1, got {app_instances()}"


def test_integration_stop_idempotent() -> None:
    _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
          "-File", str(STOP_PS1)])
    assert _wait_for_count(0)
    # stopping again with no instance must be safe and stay at 0
    result = _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(STOP_PS1)])
    assert result.returncode == 0, f"idempotent stop must exit 0, got {result.returncode}"
    assert len(app_instances()) == 0


def run_integration() -> None:
    if INTEGRATION_SKIP:
        print("Skipping integration tests: not Windows.")
        return
    test_integration_start_single()
    test_integration_repeated_start_single()
    test_integration_direct_second_launches()
    test_integration_stop_zero()
    test_integration_restart_single()
    test_integration_abnormal_kill_restart()
    test_integration_stop_idempotent()
    # leave exactly one Firefly running
    _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
          "-File", str(START_PS1)])
    assert _wait_for_count(1)
    print("Single-instance integration tests passed (1 Firefly left running).")


def main() -> None:
    run_unit()
    if "--integration" in sys.argv:
        run_integration()


if __name__ == "__main__":
    main()
