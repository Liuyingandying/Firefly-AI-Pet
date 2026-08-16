"""Write a pet state into runtime/sources/<agent>.json.

Usage:
    python tools/simulate_event.py <state> [--agent claude|codex|manual]
                                          [--source hook] [--silent]

where <state> is one of:
    idle, thinking, working, waiting, success, error, sleeping

Each agent writes only its own source file. runtime/state.json is now the
resolved display state produced by state_broker.py / app.py and is never
written here. In hook mode (--silent) nothing is printed and the exit code
is always 0 so a failed write never blocks Claude Code or Codex.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SOURCES_DIR = PROJECT_DIR / "runtime" / "sources"

VALID_STATES = {"idle", "thinking", "working", "waiting", "success", "error", "sleeping"}
VALID_AGENTS = {"claude", "codex", "manual"}


def write_state(agent, state, source):
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "agent": agent,
        "source": source,
        "state": state,
        "timestamp": int(time.time() * 1000),
    }

    dst = SOURCES_DIR / f"{agent}.json"
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, dst)


def main():
    parser = argparse.ArgumentParser(
        description="Write a pet state to runtime/sources/<agent>.json"
    )
    parser.add_argument("state", nargs="?", help="target state")
    parser.add_argument("--agent", default="manual", help="claude|codex|manual (default: manual)")
    parser.add_argument("--source", default="manual", help="origin marker (default: manual)")
    parser.add_argument("--silent", action="store_true", help="suppress stdout and always exit 0")
    args = parser.parse_args()

    silent = args.silent

    if args.state is None:
        if not silent:
            parser.print_help()
        return 0

    state = args.state.lower()
    if state not in VALID_STATES:
        if not silent:
            print(f"invalid state: {state!r}", file=sys.stderr)
            print("valid states:", ", ".join(sorted(VALID_STATES)), file=sys.stderr)
        return 1 if not silent else 0

    agent = args.agent.lower()
    if agent not in VALID_AGENTS:
        if not silent:
            print(f"invalid agent: {agent!r}", file=sys.stderr)
            print("valid agents:", ", ".join(sorted(VALID_AGENTS)), file=sys.stderr)
        return 1 if not silent else 0

    try:
        write_state(agent, state, args.source)
    except Exception as exc:
        if not silent:
            print(f"error writing state: {exc}", file=sys.stderr)
        return 1 if not silent else 0

    if not silent:
        print(f"state set to {state} (agent={agent})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
