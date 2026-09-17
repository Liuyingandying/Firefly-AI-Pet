"""Firefly LED bridge — runtime/led_state.json -> LED Node (ESP32-S3).

Standalone peripheral process: watches the resolver's final *detailed*
six-state file (``runtime/led_state.json``, written by app.py) and forwards
the states over USB serial to the LED Node:

    idle          -> STATE:IDLE
    working       -> STATE:WORKING
    tool_running  -> STATE:TOOL_RUNNING
    waiting_input -> STATE:WAITING_INPUT
    success       -> STATE:SUCCESS
    error         -> STATE:ERROR

Protocol (LED Node firmware, 115200 8N1, lines end with '\\n'):
    firmware out:  LED_NODE_READY            (boot / reboot)
    firmware out:  STATE_CHANGED:<STATE>     (state accepted = ACK)
    firmware out:  UNKNOWN_STATE             (invalid state)

Bridge guarantees:
  - 115200, automatic reconnect, graceful shutdown via --shutdown-file
  - sends only on state change (no repeat for the same state)
  - on LED_NODE_READY (device rebooted) immediately resends current state
  - a serial disconnect never affects the pet (this process only logs)
  - invalid/unreadable state file content keeps the last known state
  - latency metrics per transition: file change -> send -> ACK

This tool is fully decoupled from the pet app: it imports no Qt and no
application modules.  Run it from the supervisor alongside the Prism bridge.

Usage:
    python tools/firefly_led_bridge.py                  # watch state file
    python tools/firefly_led_bridge.py --test working   # one-shot protocol check
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

# Phase 4: LED detail state (six states).  Falls back to the four-state
# hardware file only when --state-file is passed explicitly.
DEFAULT_STATE_FILE = get_user_data_paths().runtime / "led_state.json"
DEFAULT_BAUD = 115200
POLL_INTERVAL_S = 0.05
RECONNECT_INTERVAL_S = 2.0
READY_WAIT_S = 4.0
ACK_TIMEOUT_S = 1.5
LOG_DIR = get_user_data_paths().logs / "led_bridge"
LOG_FILE = LOG_DIR / "led_bridge.log"
MAX_LOG_BYTES = 2 * 1024 * 1024

BRIDGE_MUTEX_NAME = "FireflyAIPet-LEDBridge-v1"
ERROR_ALREADY_EXISTS = 183
ALREADY_RUNNING_EXIT_CODE = 23

# Only these resolver detail states are consumed by the LED node.
VALID_STATES = frozenset(
    {"idle", "working", "tool_running", "waiting_input", "success", "error"}
)
STATE_MAP = {
    "idle": "IDLE",
    "working": "WORKING",
    "tool_running": "TOOL_RUNNING",
    "waiting_input": "WAITING_INPUT",
    "success": "SUCCESS",
    "error": "ERROR",
}

_mutex_handle: int | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


class LedBridgeLog:
    """Line-buffered bridge log (own file) plus optional stdout mirror."""

    def __init__(self) -> None:
        self._fh: object | None = None
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size >= MAX_LOG_BYTES:
                LOG_FILE.write_text("", encoding="utf-8")
            self._fh = LOG_FILE.open("a", encoding="utf-8", buffering=1)
        except OSError:
            self._fh = None

    def info(self, message: str, *, stdout: bool = True) -> None:
        line = f"[{_stamp()}] [led_bridge] {message}"
        if self._fh is not None:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass
        if stdout:
            print(line, flush=True)

    def metric(self, message: str) -> None:
        # Machine-parseable latency line, always to the log file.
        line = f"[{_stamp()}] [led_bridge] METRIC {message}"
        if self._fh is not None:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


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
    """Atomically own the LED bridge mutex for this process lifetime."""
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


def read_state(state_file: Path) -> tuple[str, int] | None:
    """Return (state, file_mtime_ms) or None when unreadable this tick.

    None covers every transient condition: file missing, mid-replace
    (atomic rename window), or invalid JSON — the caller keeps the last
    known state and waits for the next poll.
    """
    try:
        st = state_file.stat()
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    state = payload.get("state") if isinstance(payload, dict) else None
    if not isinstance(state, str) or state not in VALID_STATES:
        return None
    return state, int(st.st_mtime * 1000)


def open_port(port_name: str, baud: int) -> serial.Serial:
    ser = serial.Serial(
        port_name,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.3,
    )
    # DTR must be asserted: the LED node now runs on the ESP32-S3 native USB
    # (CDC), and the core drops every byte while the host is "not connected"
    # (HWCDC::write -> flushTXBuffer when !isCDC_Connected()).  RTS stays
    # de-asserted; resets are pulsed explicitly where the wiring supports it.
    ser.dtr = True
    ser.rts = False
    return ser


def close_handle(ser: serial.Serial | None) -> None:
    if ser is None:
        return
    try:
        ser.close()
    except Exception:
        pass


def reset_device(port: serial.Serial) -> None:
    """Pulse RTS to reboot the ESP32-S3 for a clean LED_NODE_READY.

    DTR stays asserted: on the native-USB LED node the core discards all
    output while the CDC is "not connected" (see open_port).
    """
    port.dtr = True
    port.rts = True
    time.sleep(0.1)
    port.rts = False
    port.reset_input_buffer()


def drain_lines(port: serial.Serial) -> list[str]:
    try:
        n = port.in_waiting
        if n <= 0:
            return []
        data = port.read(n).decode("utf-8", errors="replace")
        return [ln.strip() for ln in data.splitlines() if ln.strip()]
    except (serial.SerialException, OSError):
        raise


def resolve_port_name(port_arg: str) -> str | None:
    """Resolve the LED port: explicit port or config identity match."""
    if port_arg and port_arg != "auto":
        return port_arg if port_arg in {p.device for p in list_ports.comports()} else None
    from hardware_ports import resolve_port

    return resolve_port("led")


class LedStatePump:
    """State file -> LED node pump with reconnect and ACK tracking."""

    def __init__(
        self,
        state_file: Path,
        port_name: str,
        baud: int,
        shutdown: ShutdownRequest,
        log: LedBridgeLog,
    ) -> None:
        self.state_file = state_file
        self.port_name = port_name
        self.baud = baud
        self.shutdown = shutdown
        self.log = log
        self.ser: serial.Serial | None = None
        self.sent_state: str | None = None  # last state SENT (reset on reconnect)
        self._pending_send: str | None = None
        self._pending_send_at: int | None = None
        self._pending_file_mtime: int | None = None
        self._pending_observed_at: int | None = None

    # -- connection lifecycle -------------------------------------------

    def _connect(self) -> bool:
        target = resolve_port_name(self.port_name)
        if target is None:
            available = ", ".join(p.device for p in list_ports.comports()) or "none"
            self.log.info(
                f"port {self.port_name!r} not found (present: {available}); retrying",
                stdout=False,
            )
            return False
        try:
            self.ser = open_port(target, self.baud)
        except (serial.SerialException, OSError) as exc:
            self.log.info(f"cannot open {target}: {exc}; retrying", stdout=False)
            return False
        self.log.info(f"opened {target} @ {self.baud} 8N1")

        # Reset for a clean handshake; a device that stays silent (no
        # auto-reset wiring) is still usable — we resend the current state.
        try:
            reset_device(self.ser)
        except (serial.SerialException, OSError) as exc:
            self.log.info(f"reset pulse failed: {exc}; continuing")
        ready = self._wait_for_ready()
        if ready:
            self.log.info("device LED_NODE_READY (fresh boot); will resend current state")
        else:
            self.log.info("no LED_NODE_READY after reset; sending current state anyway")
        self.sent_state = None  # force a resync of the current state
        return True

    def _wait_for_ready(self) -> bool:
        deadline = time.monotonic() + READY_WAIT_S
        while time.monotonic() < deadline:
            if self.shutdown.requested():
                return False
            try:
                lines = drain_lines(self.ser)
            except (serial.SerialException, OSError) as exc:
                self.log.info(f"read during handshake failed: {exc}")
                return False
            for line in lines:
                if line == "LED_NODE_READY":
                    return True
                if line.startswith("STATE_CHANGED:"):
                    # Device already running in a known state; treat as ready.
                    return True
            if self.shutdown.wait(0.1):
                return False
        return False

    def _handle_incoming(self, lines: list[str]) -> None:
        for line in lines:
            if line == "LED_NODE_READY":
                self.log.info("device LED_NODE_READY (reboot detected); resending state")
                self.sent_state = None  # immediate resend of current state
            elif line.startswith("STATE_CHANGED:"):
                acked = line[len("STATE_CHANGED:"):]
                self._on_ack(acked)
            elif line == "UNKNOWN_STATE":
                self.log.info("device rejected a state (UNKNOWN_STATE)", stdout=False)
            else:
                self.log.info(f"device line: {line!r}", stdout=False)

    def _on_ack(self, acked: str) -> None:
        now = _now_ms()
        if self._pending_send is not None and acked == self._pending_send:
            lat_total = now - self._pending_file_mtime if self._pending_file_mtime else None
            lat_send = now - self._pending_send_at if self._pending_send_at else None
            self.log.metric(
                f"ack state={acked} file_to_ack_ms={lat_total} "
                f"send_to_ack_ms={lat_send}"
            )
            self.log.info(f"ACK STATE_CHANGED:{acked} (file->ack "
                          f"{lat_total}ms, send->ack {lat_send}ms)")
            self._pending_send = None
            self._pending_send_at = None
            self._pending_file_mtime = None
            self._pending_observed_at = None
        else:
            # Stale/other ACK (e.g. from a previous send) — ignore.
            self.log.info(f"ACK STATE_CHANGED:{acked} (no pending send)", stdout=False)

    # -- main loop ------------------------------------------------------

    def run(self) -> int:
        self.log.info(
            f"watching {self.state_file} (states: {', '.join(sorted(VALID_STATES))})"
        )
        while True:
            if self.shutdown.requested():
                self.log.info("graceful shutdown requested")
                break

            if self.ser is None:
                if not self._connect():
                    if self.shutdown.wait(RECONNECT_INTERVAL_S):
                        break
                    continue

            # Read any firmware output (ACKs, reboot banners).
            try:
                lines = drain_lines(self.ser)
            except (serial.SerialException, OSError) as exc:
                self.log.info(f"serial read failed: {exc}; reconnecting")
                close_handle(self.ser)
                self.ser = None
                if self.shutdown.wait(RECONNECT_INTERVAL_S):
                    break
                continue
            self._handle_incoming(lines)

            # Read the authoritative state file.
            observed = read_state(self.state_file)
            if observed is None:
                # Missing / mid-atomic-replace / invalid content: keep last
                # known state and wait for the next poll.
                if self.shutdown.wait(POLL_INTERVAL_S):
                    break
                continue
            state, mtime = observed

            if state != self.sent_state:
                # Protocol ACKs arrive in UPPERCASE (STATE_CHANGED:WORKING);
                # track the protocol form so the ACK comparison is exact.
                self._pending_send = STATE_MAP[state]
                self._pending_send_at = _now_ms()
                self._pending_file_mtime = mtime
                self._pending_observed_at = self._pending_send_at
                self.log.metric(
                    f"state_change state={state} file_mtime_ms={mtime} "
                    f"observed_at_ms={self._pending_send_at}"
                )
                self.log.info(f"send STATE:{STATE_MAP[state]}")
                try:
                    self.ser.write(f"STATE:{STATE_MAP[state]}\n".encode("utf-8"))
                    self.ser.flush()
                except (serial.SerialException, OSError) as exc:
                    self.log.info(f"serial write failed ({state}): {exc}; reconnecting")
                    close_handle(self.ser)
                    self.ser = None
                    if self.shutdown.wait(RECONNECT_INTERVAL_S):
                        break
                    continue

                # Wait for the ACK (interruptible by shutdown).
                acked = self._wait_ack(state)
                if not acked:
                    self.log.info(
                        f"note: STATE:{STATE_MAP[state]} unacked; "
                        f"will re-send if the device reconnects"
                    )
                self.sent_state = state
                self._pending_send = None

            if self.shutdown.wait(POLL_INTERVAL_S):
                break

        close_handle(self.ser)
        self.ser = None
        self.log.info("shutdown complete")
        return 0

    def _wait_ack(self, state: str) -> bool:
        deadline = time.monotonic() + ACK_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.shutdown.requested():
                return False
            try:
                lines = drain_lines(self.ser)
            except (serial.SerialException, OSError) as exc:
                self.log.info(f"read during ACK wait failed: {exc}")
                return False
            self._handle_incoming(lines)
            if self._pending_send is None:
                return True  # _on_ack consumed it
            if self.shutdown.wait(0.05):
                return False
        return False


def run_test(state: str, port_name: str, baud: int) -> int:
    """One-shot protocol check: wait for READY, send STATE, print response."""
    from hardware_ports import describe

    target = resolve_port_name(port_name)
    if target is None:
        print(f"[error] LED port not found. Present ports: "
              f"{', '.join(p.device for p in list_ports.comports()) or 'none'}",
              file=sys.stderr)
        return 1
    print(f"[test] LED port: {describe('led', target)}")
    try:
        ser = open_port(target, baud)
    except (serial.SerialException, OSError) as exc:
        print(f"[error] cannot open {target}: {exc}", file=sys.stderr)
        return 1
    try:
        # Opening the CH343 auto-reset circuit may reboot the device; wait
        # for the firmware banner (LED_NODE_READY) before sending.
        deadline = time.monotonic() + READY_WAIT_S
        ready = False
        while time.monotonic() < deadline:
            n = ser.in_waiting
            if n:
                data = ser.read(n).decode("utf-8", errors="replace")
                for ln in data.splitlines():
                    ln = ln.strip()
                    if ln == "LED_NODE_READY":
                        ready = True
            if ready:
                break
            time.sleep(0.05)

        ser.write(f"STATE:{STATE_MAP[state]}\n".encode("utf-8"))
        ser.flush()
        deadline = time.monotonic() + 3.0
        responses = []
        while time.monotonic() < deadline:
            n = ser.in_waiting
            if n:
                data = ser.read(n).decode("utf-8", errors="replace")
                responses += [l.strip() for l in data.splitlines() if l.strip()]
            time.sleep(0.05)
    except (serial.SerialException, OSError) as exc:
        print(f"[error] serial I/O failed on {target}: {exc}", file=sys.stderr)
        return 1
    finally:
        close_handle(ser)
    print(f"[test] sent STATE:{STATE_MAP[state]} -> {target} @ {baud} "
          f"(ready={'yes' if ready else 'no'})")
    for line in responses:
        print(f"[test] device: {line}")
    acked = any(line == f"STATE_CHANGED:{STATE_MAP[state]}" for line in responses)
    print(f"[test] {'ACKED' if acked else 'NO ACK'}")
    return 0 if acked else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Firefly hardware_state.json -> LED Node serial bridge"
    )
    parser.add_argument("--port", default="auto",
                        help="LED serial port (default: auto via config/hardware_devices.json)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--shutdown-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--test", choices=sorted(VALID_STATES), metavar="STATE",
                        help="send one STATE:<value> line and exit, without watching")
    args = parser.parse_args(argv)

    if args.test is not None:
        return run_test(args.test, args.port, args.baud)

    if not acquire_single_instance():
        print("[led_bridge] healthy bridge already owns the single-instance mutex",
              flush=True)
        return ALREADY_RUNNING_EXIT_CODE

    log = LedBridgeLog()
    log.info(f"LED bridge start pid={os.getpid()}")
    if not args.state_file.exists():
        log.info(f"warn: {args.state_file} does not exist yet; waiting for the pet app")
    try:
        return LedStatePump(
            args.state_file, args.port, args.baud,
            ShutdownRequest(args.shutdown_file), log,
        ).run()
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
