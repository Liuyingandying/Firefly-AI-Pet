"""Multi-agent state broker for the Firefly AI Desktop Pet.

Reads per-agent source files from runtime/sources/*.json and resolves a
single display state. Pure Python (no Qt) so it can be unit-tested easily.

Each agent writes only its own source file (claude.json / codex.json /
manual.json). This module merges those into a final display state using
priority + per-agent timestamp, so no cross-agent timers can overwrite
each other.
"""

import json
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SOURCES_DIR = PROJECT_DIR / "runtime" / "sources"

VALID_STATES = {"idle", "thinking", "working", "waiting", "success", "error", "sleeping"}
AGENTS = ("claude", "codex", "manual")

# Higher priority wins.
PRIORITY = {
    "waiting": 7,
    "error": 6,
    "success": 5,
    "working": 4,
    "thinking": 3,
    "idle": 2,
    "sleeping": 1,
}

# Transient states revert to idle once their source timestamp is this old.
TRANSIENT_TTL_MS = {
    "success": 3_000,
    "error": 4_000,
}

# A manual (test) override stops affecting resolution after this long.
MANUAL_TTL_MS = 10_000

# Busy states that can get stuck after a crash are demoted to idle after
# this long with no update. Conservative crash fallback only; the normal
# SessionEnd -> sleeping path is the primary mechanism.
STALE_THRESHOLD_MS = 30 * 60 * 1000
STALE_PRONE_STATES = {"working", "thinking", "waiting"}


def _effective_state(agent, state, timestamp, now_ms):
    age = now_ms - timestamp

    if state in TRANSIENT_TTL_MS:
        if age > TRANSIENT_TTL_MS[state]:
            return "idle"
        return state

    if agent == "manual":
        if age > MANUAL_TTL_MS:
            return "sleeping"
        return state

    if state in STALE_PRONE_STATES:
        if age > STALE_THRESHOLD_MS:
            return "idle"
        return state

    return state


def _read_agent(agent, now_ms, sources_dir):
    path = sources_dir / f"{agent}.json"
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    state = data.get("state")
    if state not in VALID_STATES:
        return None
    timestamp = data.get("timestamp", 0)
    source = data.get("source", "manual")

    return {
        "agent": agent,
        "state": _effective_state(agent, state, timestamp, now_ms),
        "source": source,
        "timestamp": timestamp,
    }


def resolve(now_ms=None, sources_dir=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    sources_dir = Path(sources_dir) if sources_dir is not None else SOURCES_DIR

    entries = [
        e for e in (_read_agent(a, now_ms, sources_dir) for a in AGENTS) if e is not None
    ]

    if not entries:
        return {
            "state": "idle",
            "agent": None,
            "source": "manual",
            "timestamp": 0,
            "resolved_at": now_ms,
        }

    winner = max(entries, key=lambda e: (PRIORITY[e["state"]], e["timestamp"]))

    return {
        "state": winner["state"],
        "agent": winner["agent"],
        "source": winner["source"],
        "timestamp": winner["timestamp"],
        "resolved_at": now_ms,
    }
