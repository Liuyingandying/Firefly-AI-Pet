"""Automated tests for state_broker.py (pure Python, no Qt)."""

import json
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import state_broker

NOW = 1_000_000_000


def reset(d):
    for p in Path(d).glob("*.json"):
        p.unlink()


def write(d, agent, state, timestamp, source="hook"):
    Path(d, f"{agent}.json").write_text(
        json.dumps({"agent": agent, "source": source, "state": state, "timestamp": timestamp}),
        encoding="utf-8",
    )


def check(cond, msg):
    if cond:
        print(f"PASS: {msg}")
    else:
        print(f"FAIL: {msg}")
        sys.exit(1)


def run():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)

        # Test 1: working vs sleeping
        reset(d); write(d, "claude", "working", NOW); write(d, "codex", "sleeping", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "working" and r["agent"] == "claude", "T1 working/claude")

        # Test 2: working vs waiting -> waiting wins
        reset(d); write(d, "claude", "working", NOW); write(d, "codex", "waiting", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "waiting" and r["agent"] == "codex", "T2 waiting/codex")

        # Test 3: error vs waiting -> waiting wins
        reset(d); write(d, "claude", "error", NOW); write(d, "codex", "waiting", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "waiting" and r["agent"] == "codex", "T3 waiting/codex")

        # Test 4: error vs working -> error wins (within TTL)
        reset(d); write(d, "claude", "error", NOW); write(d, "codex", "working", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "error" and r["agent"] == "claude", "T4 error/claude")

        # Test 5: success TTL
        reset(d); write(d, "claude", "success", NOW - 1000); write(d, "codex", "working", NOW - 500)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "success" and r["agent"] == "claude", "T5 success within TTL")
        r = state_broker.resolve(now_ms=NOW + 4000, sources_dir=d)
        check(r["state"] == "working" and r["agent"] == "codex", "T5 success expired -> working/codex")

        # Test 6: same priority, newer timestamp wins
        reset(d); write(d, "claude", "working", NOW - 100); write(d, "codex", "working", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "working" and r["agent"] == "codex", "T6 working/codex (newer)")

        # Test 7: all sleeping
        reset(d); write(d, "claude", "sleeping", NOW); write(d, "codex", "sleeping", NOW - 1)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "sleeping", "T7 sleeping")

        # Test 8: idle vs sleeping -> idle
        reset(d); write(d, "claude", "idle", NOW); write(d, "codex", "sleeping", NOW - 1)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "idle" and r["agent"] == "claude", "T8 idle/claude")

        # Test 9: corrupt codex + valid claude
        reset(d); Path(d, "codex.json").write_text("{ not valid json", encoding="utf-8")
        write(d, "claude", "working", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "working" and r["agent"] == "claude", "T9 corrupt codex ignored")

        # Test 10: half-written / invalid state -> no crash
        reset(d); Path(d, "claude.json").write_text('{"state": "wor', encoding="utf-8")
        write(d, "codex", "idle", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "idle" and r["agent"] == "codex", "T10 truncated claude ignored")
        reset(d); write(d, "claude", "bogus_state", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "idle" and r["agent"] is None, "T10 invalid state -> default idle")

        # Test 11: manual override TTL
        reset(d); write(d, "claude", "working", NOW); write(d, "manual", "waiting", NOW, source="manual")
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "waiting" and r["agent"] == "manual", "T11 manual override")
        r = state_broker.resolve(now_ms=NOW + 11_000, sources_dir=d)
        check(r["state"] == "working" and r["agent"] == "claude", "T11 manual expired -> claude")

        # Test 12: error transient does not force global idle
        reset(d); write(d, "claude", "error", NOW); write(d, "codex", "working", NOW)
        r = state_broker.resolve(now_ms=NOW, sources_dir=d)
        check(r["state"] == "error" and r["agent"] == "claude", "T12 error initially")
        r = state_broker.resolve(now_ms=NOW + 5000, sources_dir=d)
        check(r["state"] == "working" and r["agent"] == "codex", "T12 error expired -> working/codex")

    print("ALL 12 BROKER TESTS PASSED")


if __name__ == "__main__":
    run()
