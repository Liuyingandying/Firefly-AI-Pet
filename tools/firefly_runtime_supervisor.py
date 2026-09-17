"""Lifecycle supervisor for the optional ESP32 bridges and Firefly desktop app.

The bridges remain separate processes.  Failure to start or connect one of
them is logged but never blocks the pet or the other bridge.  Only bridge
processes created by this supervisor are stopped; an already-running bridge
is reused and left alone.

Two independent hardware consumers, sharing the same state source
(runtime/hardware_state.json) but never sharing a COM port:

    PetStateResolver -> hardware_state.json
                            |-- Prism bridge -> Prism ESP32 (LCD/HUD)
                            `-- LED bridge   -> LED ESP32-S3 (WS2812B)

Each bridge gets its own Job Object and its own graceful shutdown file;
one failing never blocks the other.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import io
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import IO, Sequence

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.user_paths import get_user_data_paths

PYTHON = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
PYTHONW = PROJECT_DIR / ".venv" / "Scripts" / "pythonw.exe"
APP_SCRIPT = PROJECT_DIR / "app.py"
BRIDGE_SCRIPT = PROJECT_DIR / "tools" / "esp32_serial_bridge_poc.py"
LED_BRIDGE_SCRIPT = PROJECT_DIR / "tools" / "firefly_led_bridge.py"
RUNTIME_DIR = get_user_data_paths().runtime
LOG_DIR = get_user_data_paths().logs / "esp32_bridge"
LOG_FILE = LOG_DIR / "bridge_current.log"
MAX_LOG_BYTES = 2 * 1024 * 1024
BRIDGE_ALREADY_RUNNING = 23
GRACEFUL_TIMEOUT_S = 3.0


def _stamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _write_log(stream: IO[str] | None, message: str) -> None:
    if stream is None:
        return
    try:
        stream.write(f"[{_stamp()}] [supervisor] {message}\n")
        stream.flush()
    except OSError:
        pass


def _open_log() -> IO[str] | None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size >= MAX_LOG_BYTES:
            LOG_FILE.write_text("", encoding="utf-8")
        return LOG_FILE.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return None


def _no_window_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class BridgeJob:
    """Small Windows Job Object wrapper; closing it kills only its members."""

    def __init__(self, process: subprocess.Popen, log: IO[str] | None):
        self.handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            _write_log(log, "warning: CreateJobObjectW failed")
            return

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
        ]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ok = kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        )
        assigned = ok and kernel32.AssignProcessToJobObject(
            handle, ctypes.c_void_p(int(process._handle))
        )
        if not assigned:
            _write_log(log, "warning: bridge Job Object assignment failed")
            kernel32.CloseHandle(handle)
            return
        self.handle = handle
        _write_log(log, "bridge assigned to kill-on-close Job Object")

    def close(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def _signal_shutdown(path: Path, log: IO[str] | None) -> None:
    try:
        path.write_text("shutdown\n", encoding="ascii")
        _write_log(log, f"graceful shutdown signalled via {path.name}")
    except OSError as exc:
        _write_log(log, f"shutdown signal write failed: {exc}")


def run_supervised(
    pet_command: Sequence[str],
    bridge_command: Sequence[str],
    shutdown_file: Path,
    *,
    log: IO[str] | None = None,
) -> int:
    """Original single-bridge entry point (kept for tests and stability)."""
    return run_supervised_multi(
        pet_command, [(bridge_command, shutdown_file, log)], log=log
    )


def run_supervised_multi(
    pet_command: Sequence[str],
    bridge_specs: Sequence[tuple[Sequence[str], Path, object]],
    *,
    log: IO[str] | None = None,
) -> int:
    """Run the pet plus any number of independent bridge children.

    Each spec is (command, shutdown_file, output_stream).  Every bridge is
    owned by its own kill-on-close Job Object and stopped gracefully when
    the pet exits; a bridge that already ran (exit code
    BRIDGE_ALREADY_RUNNING) is reused and left alone.  A bridge start or
    runtime failure never blocks the pet or the other bridges.
    """
    bridges: list[tuple[subprocess.Popen, BridgeJob, Path]] = []
    try:
        for command, shutdown_file, child_output in bridge_specs:
            try:
                bridge_env = os.environ.copy()
                bridge_env["PYTHONIOENCODING"] = "utf-8"
                stream = child_output
                if stream is not None:
                    try:
                        stream.fileno()
                    except (AttributeError, OSError, io.UnsupportedOperation):
                        stream = None
                bridge = subprocess.Popen(
                    list(command),
                    cwd=PROJECT_DIR,
                    stdin=subprocess.DEVNULL,
                    stdout=stream if stream is not None else subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    creationflags=_no_window_flags(),
                    env=bridge_env,
                )
                job = BridgeJob(bridge, log)
                bridges.append((bridge, job, shutdown_file))
                _write_log(log, f"bridge start PID={bridge.pid} cmd={Path(command[1]).name}")
            except (OSError, ValueError) as exc:
                _write_log(log, f"bridge start failed (pet continues): {exc}")

        try:
            pet = subprocess.Popen(
                list(pet_command),
                cwd=PROJECT_DIR,
                creationflags=_no_window_flags(),
            )
        except (OSError, ValueError) as exc:
            _write_log(log, f"pet start failed: {exc}")
            return 1

        _write_log(log, f"pet start PID={pet.pid}")
        pet_code = pet.wait()
        _write_log(log, f"pet ended exit_code={pet_code}; bridge shutdown reason=pet ended")
        return pet_code
    finally:
        for bridge, job, shutdown_file in bridges:
            if bridge is not None:
                current = bridge.poll()
                if current is None:
                    _signal_shutdown(shutdown_file, log)
                    try:
                        bridge.wait(timeout=GRACEFUL_TIMEOUT_S)
                        _write_log(log, f"bridge graceful termination PID={bridge.pid}")
                    except subprocess.TimeoutExpired:
                        _write_log(log, f"bridge graceful timeout; forcing exact owned PID={bridge.pid}")
                        if job is not None and job.handle is not None:
                            job.close()
                        else:
                            bridge.kill()
                        try:
                            bridge.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            _write_log(log, "bridge exact-PID termination did not confirm")
                elif current == BRIDGE_ALREADY_RUNNING:
                    _write_log(log, "existing bridge detected; reused and not owned")
                else:
                    _write_log(log, f"bridge had already exited code={current}")
            if job is not None:
                job.close()
            try:
                shutdown_file.unlink(missing_ok=True)
            except OSError:
                pass


def _resolve_bridge_port(target: str, fallback: str) -> str:
    """Resolve a hardware port from config/hardware_devices.json (best effort).

    Falls back to ``fallback`` (the historical Prism default) when the
    config is missing or the device is not currently enumerable.
    """
    try:
        from hardware_ports import resolve_port

        port = resolve_port(target)
        if port:
            return port
    except Exception:
        pass
    return fallback


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Firefly app + optional ESP32 bridge supervisor"
    )
    parser.add_argument(
        "--bridge-port",
        default=None,
        help="Prism serial port. Default: resolve from config/hardware_devices.json; "
             "when the Prism device is absent the Prism bridge is skipped entirely "
             "so it can never grab the LED node's COM port.",
    )
    parser.add_argument("--led-bridge-port", default="auto",
                        help="LED serial port (default: auto via config/hardware_devices.json)")
    args = parser.parse_args(argv)

    log = _open_log()
    _write_log(log, f"supervisor start PID={os.getpid()}")

    led_port = _resolve_bridge_port("led", args.led_bridge_port)

    prism_shutdown = RUNTIME_DIR / f"esp32_bridge_shutdown_{os.getpid()}_{uuid.uuid4().hex}.signal"
    led_shutdown = RUNTIME_DIR / f"led_bridge_shutdown_{os.getpid()}_{uuid.uuid4().hex}.signal"

    bridge_specs: list[tuple[Sequence[str], Path, object]] = []
    if args.bridge_port is not None:
        prism_port = args.bridge_port
        _write_log(log, f"Prism bridge uses explicit --bridge-port {prism_port}")
    else:
        prism_port = _resolve_bridge_port("prism", None)
    if prism_port:
        # Prism bridge: identical behavior to the historical single-bridge
        # supervisor (stdout mirrored to the supervisor log stream).
        bridge_specs.append(
            (
                [str(PYTHON), str(BRIDGE_SCRIPT), "--port", prism_port,
                 "--shutdown-file", str(prism_shutdown)],
                prism_shutdown,
                log,
            )
        )
    else:
        _write_log(log, "Prism device not present; Prism bridge skipped "
                        "(no COM fallback, so it can never grab the LED port)")

    # LED bridge: always starts (identity-resolved, so it only ever opens
    # the LED node's port and retries until that device appears).  It logs
    # to its own file (logs/led_bridge/) so the two streams never interleave.
    bridge_specs.append(
        (
            [str(PYTHON), str(LED_BRIDGE_SCRIPT), "--port", led_port,
             "--shutdown-file", str(led_shutdown)],
            led_shutdown,
            None,
        )
    )

    pet_command = [str(PYTHONW), str(APP_SCRIPT)]
    _write_log(log, f"prism port={prism_port} led port={led_port}")
    try:
        return run_supervised_multi(pet_command, bridge_specs, log=log)
    finally:
        _write_log(log, "supervisor exit")
        if log is not None:
            log.close()


if __name__ == "__main__":
    sys.exit(main())
