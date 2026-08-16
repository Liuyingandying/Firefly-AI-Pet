"""Phase 8C.2 one-call online smoke — real Claude stream -> AgentEvent -> UI.

Exactly ONE extremely short Claude ask (``Reply exactly: OK``, isolated
``--safe-mode``) through the production chain. Verifies the NEW adapter path
end to end:

  - adapter.feed_line() maps real stream-json lines to AgentEvents
  - the agent_event signal carries SESSION / TEXT_DELTA / FINAL
  - finished text contains OK, exit 0
  - runtime/sources/claude.json is unchanged (hook isolation preserved)

Codex online calls: 0.

Usage:
  python tools/smoke_phase8c2_agent_events.py [--workspace E:\\Firefly_AI_Pet]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEventType
from core.session_manager import SessionManager
from ui.process_launcher import QuickAskRunner

PROMPT = "Reply exactly: OK"
SOURCE_FILE = PROJECT_DIR / "runtime" / "sources" / "claude.json"


def snapshot_source() -> str:
    try:
        return SOURCE_FILE.read_text(encoding="utf-8")
    except OSError:
        return "<missing>"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(PROJECT_DIR))
    args = parser.parse_args()
    workspace = Path(args.workspace)
    if not workspace.is_dir():
        print(f"workspace missing: {workspace}")
        return 2

    before = snapshot_source()
    app = QApplication.instance() or QApplication([])
    mgr = SessionManager()
    runner = QuickAskRunner(session_manager=mgr, parent=app)

    events = []
    statuses = []
    partials = []
    results = {"text": "", "exit": None, "failed": None}
    telemetry = []
    runner.agent_event.connect(events.append)
    runner.status.connect(statuses.append)
    runner.partial.connect(partials.append)
    runner.finished.connect(lambda text, code: (results.update(text=text, exit=code), loop.quit()))
    runner.failed.connect(lambda msg: (results.update(failed=msg), loop.quit()))
    runner.telemetry.connect(telemetry.append)
    loop = QEventLoop()

    t0 = time.time()
    ok = runner.ask("claude", PROMPT, workspace, effort="low", persistent=True, isolated=True)
    if not ok:
        print("[smoke] FAIL: ask() returned False")
        return 1
    loop.exec()
    wall_ms = (time.time() - t0) * 1000

    types = {ev.type for ev in events}
    text_deltas = [ev.text for ev in events if ev.type == AgentEventType.TEXT_DELTA and ev.text]
    finals = [ev.text for ev in events if ev.type == AgentEventType.FINAL and ev.text]
    session_id = mgr.get_native_id("claude", workspace)
    after = snapshot_source()

    print(f"[smoke] wall_ms: {wall_ms:.0f}")
    print(f"[smoke] agent_event types: {sorted(t.name for t in types)}")
    print(f"[smoke] deltas: {len(text_deltas)}  finals: {len(finals)}")
    print(f"[smoke] final text: {results['text'][:120]!r}")
    print(f"[smoke] exit: {results['exit']}  failed: {results['failed']}")
    print(f"[smoke] status tail: {statuses[-3:]}")
    print(f"[smoke] session saved: {bool(session_id)}  source_unchanged: {before == after}")
    if telemetry:
        p = telemetry[-1].to_payload()
        print(
            f"[smoke] telemetry: first_event={p['derived']['first_event_ms']}ms "
            f"first_text={p['derived']['first_text_ms']}ms total={p['derived']['total_ms']}ms "
            f"t5={telemetry[-1].milestone_t5 is not None}"
        )

    bad = []
    if AgentEventType.SESSION not in types:
        bad.append("no SESSION event from real stream")
    if not (text_deltas or finals):
        bad.append("no text (TEXT_DELTA/FINAL) from real stream")
    if results["failed"] or results["exit"] != 0 or "OK" not in (results["text"] or ""):
        bad.append("turn did not return OK")
    if before != after:
        bad.append("internal ask rewrote runtime/sources/claude.json")
    if not telemetry or telemetry[-1].milestone_t5 is None:
        bad.append("telemetry T5 not driven by first TEXT_DELTA")

    if bad:
        print("[smoke] FAIL: " + "; ".join(bad))
        return 1
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
