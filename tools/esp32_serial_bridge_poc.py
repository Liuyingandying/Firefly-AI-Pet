"""Standalone PC -> ESP32-S3 serial state bridge (PoC).

Reads ``runtime/state.json`` — the resolved-state snapshot already written
atomically by ``core/state_monitor.StateMonitor._write_resolved`` — and
forwards lifecycle state changes over a USB serial line to an ESP32-S3.

This tool is fully decoupled from the pet app: it does not import the
application, touch ``app.py``, or require the Qt event loop. The app keeps
running untouched; this script only observes the existing state file.

Wire protocol (minimal v1):
    STATE:idle\n
    STATE:working\n
    ...
One UTF-8 line per state change, only sent when the resolved state differs
from the last successfully sent state, at 115200 8N1 by default.

Usage:
    python tools/esp32_serial_bridge_poc.py                 # watch state.json
    python tools/esp32_serial_bridge_poc.py --test working  # one-shot send
    python tools/esp32_serial_bridge_poc.py --port COM5 --baud 115200
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.user_paths import get_user_data_paths

# Default: the resolver's final display state (written by app.py's
# _write_hardware_state). Falls back to the legacy resolved snapshot when
# --state-file runtime/state.json is passed explicitly.
DEFAULT_STATE_FILE = get_user_data_paths().runtime / "hardware_state.json"
DEFAULT_PORT = "COM5"
DEFAULT_BAUD = 115200
POLL_INTERVAL_S = 0.3
RECONNECT_INTERVAL_S = 2.0
BRIDGE_MUTEX_NAME = "FireflyAIPet-ESP32SerialBridge-v1"
ERROR_ALREADY_EXISTS = 183
ALREADY_RUNNING_EXIT_CODE = 23

# Mirror of core.models.LifecycleState; kept literal so this tool never
# imports the application stack.
VALID_STATES = frozenset(
    {"idle", "thinking", "working", "waiting", "success", "error", "sleeping"}
)

_mutex_handle: int | None = None


class ShutdownRequest:
    """Cooperative, process-local shutdown backed by a supervisor signal file."""

    def __init__(self, path: Path | None = None):
        self.path = path

    def requested(self) -> bool:
        return self.path is not None and self.path.exists()

    def wait(self, seconds: float) -> bool:
        """Wait up to ``seconds``; return True as soon as shutdown is requested."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.requested():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))
        return True


def acquire_single_instance() -> bool:
    """Atomically own the bridge mutex for this process lifetime on Windows."""
    global _mutex_handle
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, BRIDGE_MUTEX_NAME)
    if not handle:
        return False
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def read_state(state_file: Path) -> str | None:
    """Return the resolved lifecycle state, or None if unreadable this tick.

    None covers every transient condition: state.json missing entirely, the
    atomic replace mid-flight, and a torn/invalid JSON payload. Callers simply
    keep the last known state and retry on the next poll.
    """
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    state = payload.get("state")
    return state if state in VALID_STATES else None


def send_state(port: serial.Serial, state: str) -> None:
    port.write(f"STATE:{state}\n".encode("utf-8"))
    port.flush()


def drain_lines(port: serial.Serial) -> list[str]:
    """Read whatever is buffered and return clean lines."""
    n = port.in_waiting
    if not n:
        return []
    data = port.read(n).decode("utf-8", errors="replace")
    return [l.strip() for l in data.replace("\r\n", "\n").split("\n") if l.strip()]


def close_handle(ser: serial.Serial | None) -> None:
    """Best-effort close of a serial handle; a dead port must not raise."""
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass


def wait_for_line(
    port: serial.Serial,
    predicate,
    timeout: float,
    shutdown: ShutdownRequest | None = None,
):
    """Collect lines until predicate matches or timeout. Returns (ok, seen)."""
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        if shutdown is not None and shutdown.requested():
            return False, seen
        for line in drain_lines(port):
            seen.append(line)
            if predicate(line):
                return True, seen
        time.sleep(0.02)
    return False, seen


def reset_device(port: serial.Serial) -> None:
    """DTR/RTS pulse -> ESP32-S3 reset, so we get a fresh READY + boot anim."""
    port.dtr = False
    port.rts = True
    time.sleep(0.15)
    port.rts = False
    time.sleep(0.05)
    port.reset_input_buffer()


def handshake(port: serial.Serial, shutdown: ShutdownRequest | None = None) -> bool:
    """Reset the device and wait for READY. Returns True if seen."""
    try:
        reset_device(port)
    except (serial.SerialException, OSError):
        return False
    ok, _ = wait_for_line(port, lambda l: l == "READY", 12.0, shutdown)
    return ok


def send_and_confirm(
    port: serial.Serial,
    state: str,
    shutdown: ShutdownRequest | None = None,
) -> bool:
    """Send STATE and wait for ACK:STATE:<state>. Returns ACK status."""
    t0 = time.time()
    port.write(f"STATE:{state}\n".encode("utf-8"))
    port.flush()
    ok, _ = wait_for_line(
        port, lambda l: l == f"ACK:STATE:{state}", 2.0, shutdown
    )
    print(f"[bridge] STATE:{state} {'ACK ' + str(round((time.time()-t0)*1000)) + 'ms' if ok else 'NO-ACK'}",
          flush=True)
    return ok


def open_port(port_name: str, baud: int) -> serial.Serial:
    ser = serial.Serial(port=None if port_name == "auto" else port_name,
                        baudrate=baud,
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=1.0)
    # Keep both control lines de-asserted so the CH343 auto-reset circuit
    # leaves the chip running; we pulse RTS explicitly for resets.
    ser.dtr = False
    ser.rts = False
    return ser


def resolve_auto_port(port_name: str) -> str | None:
    """Pick the best guess for an ESP32-S3 USB CDC/JTAG serial device."""
    ports = {p.device: p.description for p in list_ports.comports()}
    if port_name != "auto":
        if port_name in ports:
            return port_name
        return None
    for device, description in sorted(ports.items()):
        text = description.lower()
        if any(marker in text for marker in ("esp32", "jtag", "usb serial", "cdc")):
            return device
    return None


def run_watch(
    state_file: Path,
    port_name: str,
    baud: int,
    shutdown: ShutdownRequest | None = None,
) -> int:
    shutdown = shutdown or ShutdownRequest()
    ser: serial.Serial | None = None
    sent_state: str | None = None
    print(f"[bridge] watching {state_file}")
    print(f"[bridge] valid states: {', '.join(sorted(VALID_STATES))}")
    try:
        while True:
            if shutdown.requested():
                print("[bridge] graceful shutdown requested", flush=True)
                break
            if ser is None:
                target = resolve_auto_port(port_name)
                if target is None:
                    available = ", ".join(p.device for p in list_ports.comports()) or "none"
                    print(f"[bridge] serial port not available "
                          f"(wanted {port_name}; present: {available})", flush=True)
                    if shutdown.wait(RECONNECT_INTERVAL_S):
                        continue
                    continue
                try:
                    ser = open_port(target, baud)
                except (serial.SerialException, OSError) as exc:
                    print(f"[bridge] cannot open {target}: {exc}", flush=True)
                    if shutdown.wait(RECONNECT_INTERVAL_S):
                        continue
                    continue
                sent_state = None
                print(f"[bridge] opened {target} @ {baud} 8N1", flush=True)
                try:
                    ready = handshake(ser, shutdown)
                except (serial.SerialException, OSError) as exc:
                    print(f"[bridge] handshake failed: {exc}; reconnecting", flush=True)
                    close_handle(ser)
                    ser = None
                    if shutdown.wait(RECONNECT_INTERVAL_S):
                        continue
                    continue
                if ready:
                    print("[bridge] device READY (fresh boot)", flush=True)
                else:
                    print("[bridge] no READY after reset; sending states anyway", flush=True)

            # If the ESP32 rebooted it prints READY; re-arm so the current
            # state from state.json is resent immediately (self-heal).  A
            # transient serial error (device unplug / USB re-enumerate) must
            # NOT kill the process: drop the handle and re-enter the reconnect
            # loop, which re-reads state.json and resends it after READY.
            try:
                for line in drain_lines(ser):
                    if line == "READY":
                        print("[bridge] device READY (reboot detected) - resending state",
                              flush=True)
                        sent_state = None
                    elif line.startswith("ERR:"):
                        print(f"[bridge] device: {line}", flush=True)
            except (serial.SerialException, OSError) as exc:
                print(f"[bridge] serial read failed: {exc}; reconnecting", flush=True)
                close_handle(ser)
                ser = None
                if shutdown.wait(RECONNECT_INTERVAL_S):
                    continue
                continue

            state = read_state(state_file)
            if state is not None and state != sent_state:
                try:
                    acked = send_and_confirm(ser, state, shutdown)
                except (serial.SerialException, OSError) as exc:
                    # Device unplugged or port seized by another process;
                    # drop the handle and re-enter the reconnect loop.
                    print(f"[bridge] serial write failed ({state}): {exc}", flush=True)
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                    if shutdown.wait(RECONNECT_INTERVAL_S):
                        continue
                    continue
                sent_state = state
                if not acked:
                    print(f"[bridge] note: STATE:{state} unacked; "
                          f"will re-send if the device reconnects", flush=True)

            shutdown.wait(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print("\n[bridge] stopped by user", flush=True)
    finally:
        if ser is not None:
            try:
                ser.close()
                print("[bridge] serial port closed", flush=True)
            except Exception:
                pass
        print("[bridge] shutdown complete", flush=True)
    return 0


def run_test(state: str, port_name: str, baud: int) -> int:
    target = resolve_auto_port(port_name)
    if target is None:
        available = ", ".join(p.device for p in list_ports.comports()) or "none"
        print(f"[error] serial port {port_name!r} not found. Present ports: {available}",
              file=sys.stderr)
        return 1
    try:
        ser = open_port(target, baud)
    except (serial.SerialException, OSError) as exc:
        print(f"[error] cannot open {target} (port missing, busy, or unplugged): {exc}",
              file=sys.stderr)
        return 1
    try:
        send_state(ser, state)
    except (serial.SerialException, OSError) as exc:
        print(f"[error] write failed on {target}: {exc}", file=sys.stderr)
        return 1
    finally:
        ser.close()
    print(f"[test] sent STATE:{state} -> {target} @ {baud}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Firefly state.json -> ESP32-S3 serial bridge PoC")
    parser.add_argument("--port", default=DEFAULT_PORT,
                        help=f"serial port (default: {DEFAULT_PORT}; use 'auto' to sniff)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument(
        "--shutdown-file",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--test", choices=sorted(VALID_STATES), metavar="STATE",
                        help="send one STATE:<value> line and exit, without watching state.json")
    args = parser.parse_args()

    if args.test is not None:
        return run_test(args.test, args.port, args.baud)

    if not acquire_single_instance():
        print("[bridge] healthy bridge already owns the single-instance mutex", flush=True)
        return ALREADY_RUNNING_EXIT_CODE

    if not args.state_file.exists():
        print(f"[warn] {args.state_file} does not exist yet; "
              f"waiting for the pet app to create it", flush=True)
    return run_watch(
        args.state_file,
        args.port,
        args.baud,
        ShutdownRequest(args.shutdown_file),
    )


if __name__ == "__main__":
    sys.exit(main())
