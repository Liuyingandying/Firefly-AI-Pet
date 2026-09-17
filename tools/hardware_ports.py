"""Shared USB-serial port resolution for Firefly hardware bridges.

Reads ``config/hardware_devices.json`` and resolves each named hardware
target (prism / led) to a COM port using stable device identity:

  1. explicit ``port`` (if set and currently present)
  2. USB serial match (authoritative; stable across reboots)
  3. VID:PID match (only among devices not already claimed by a target
     with a stronger serial match)

Pure Python + pyserial; must never import Qt or the application stack so
both bridges and the supervisor can use it as a peripheral process helper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import serial.tools.list_ports as list_ports

PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_FILE = PROJECT_DIR / "config" / "hardware_devices.json"

# Matches the serial number shown in hwid like
# "USB VID:PID=XXXX:YYYY SER=<device-serial> LOCATION=<usb-location>"
_HWID_SER_PREFIX = "SER="


def _parse_hwid_serial(hwid: str) -> str | None:
    for part in hwid.split():
        if part.startswith(_HWID_SER_PREFIX):
            return part[len(_HWID_SER_PREFIX):]
    return None


def enumerate_devices() -> list[dict[str, Any]]:
    """Return all present serial ports as identity dicts."""
    devices = []
    for p in list_ports.comports():
        serial = (p.serial_number or "").strip() or _parse_hwid_serial(p.hwid or "")
        vid_pid = None
        if p.vid is not None and p.pid is not None:
            vid_pid = f"{p.vid:04X}:{p.pid:04X}"
        devices.append(
            {
                "device": p.device,
                "description": p.description or "",
                "vid_pid": vid_pid,
                "serial": serial,
                "hwid": p.hwid or "",
                "manufacturer": p.manufacturer or "",
            }
        )
    return devices


def _matches(device: dict[str, Any], match: dict[str, Any] | None) -> bool:
    if not match:
        return True
    vid_pid = match.get("vid_pid")
    if vid_pid and device.get("vid_pid") != vid_pid:
        return False
    serial = match.get("serial")
    if serial and device.get("serial") != serial:
        return False
    return True


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_port(target: str, *, config: dict[str, Any] | None = None) -> str | None:
    """Resolve ``target`` (prism/led) to a COM device name, or None."""
    config = load_config() if config is None else config
    entry = config.get(target) or {}
    if not isinstance(entry, dict):
        return None

    devices = enumerate_devices()
    by_device = {d["device"]: d for d in devices}

    # 1. Explicit port wins when the port is actually present.
    explicit = entry.get("port")
    if explicit and explicit != "auto" and explicit in by_device:
        return explicit

    # 2. Devices claimed by stronger (serial) matches are excluded from
    #    weaker (VID:PID only) matches so two same-model bridges never
    #    grab the same COM.
    claimed: set[str] = set()
    for other_name, other_entry in config.items():
        if not isinstance(other_entry, dict) or other_name == target:
            continue
        other_match = other_entry.get("match") or {}
        other_serial = other_match.get("serial")
        if other_serial:
            for d in devices:
                if d.get("serial") == other_serial:
                    claimed.add(d["device"])

    match = entry.get("match") or {}
    serial = match.get("serial")
    for d in devices:
        if d["device"] in claimed:
            continue
        if not _matches(d, match):
            continue
        if serial:
            return d["device"]  # serial match is authoritative
    # 3. VID:PID-only fallback among unclaimed devices.
    for d in devices:
        if d["device"] in claimed:
            continue
        if _matches(d, match):
            return d["device"]
    return None


def describe(target: str, port: str | None) -> str:
    """Human-readable identity of the resolved port for logs."""
    for d in enumerate_devices():
        if d["device"] == port:
            return (
                f"{port} ({d['description']} vid_pid={d['vid_pid']} "
                f"serial={d['serial']} mfg={d['manufacturer']})"
            )
    return f"{port} (not enumerated)"


if __name__ == "__main__":
    import sys

    targets = sys.argv[1:] or ["prism", "led"]
    for t in targets:
        port = resolve_port(t)
        print(f"{t}: {describe(t, port) if port else 'NOT FOUND'}")
