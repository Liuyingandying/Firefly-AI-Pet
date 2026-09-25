"""Phase 8C.1 online smoke — exactly 2 extremely short Claude asks.

Drives the REAL production chain end to end: QuickAskRunner.ask(isolated=True)
-> PowerShell wrapper -> claude.CMD -> native claude.exe --safe-mode, with the
settings.json env block injected into the child. Verifies:
  call 1: new native session, first text + total timings,
  call 2: --resume of the same session,
and that runtime/sources/claude.json is NOT rewritten by either internal ask
(external lifecycle preserved). Codex: 0 online calls.

Usage:
  python tools/smoke_phase8c1_short_ask.py [--workspace <仓库目录>]
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

from core.quick_ask_metrics import MetricsWriter
from core.session_manager import SessionManager
from ui.process_launcher import QuickAskRunner

PROMPT = "Reply exactly: OK"
SOURCE_FILE = PROJECT_DIR / "runtime" / "sources" / "claude.json"


def snapshot_source() -> str:
    try:
        return SOURCE_FILE.read_text(encoding="utf-8")
    except OSError:
        return "<missing>"


def run_turn(app: QApplication, runner: QuickAskRunner, workspace: Path, label: str) -> dict:
    before = snapshot_source()
    results: dict = {"status": [], "text": "", "exit": None, "failed": None}
    loop = QEventLoop()

    runner.status.connect(lambda s: results["status"].append(s))
    runner.partial.connect(lambda d: results.setdefault("stream", []).append(d))
    runner.finished.connect(lambda text, code: (results.update(text=text, exit=code), loop.quit()))
    runner.failed.connect(lambda msg: (results.update(failed=msg), loop.quit()))

    t0 = time.time()
    ok = runner.ask("claude", PROMPT, workspace, effort="low", persistent=True, isolated=True)
    if not ok:
        results["failed"] = results["failed"] or "ask() returned False"
        loop.quit()
    else:
        loop.exec()

    wall_ms = (time.time() - t0) * 1000
    results["wall_ms"] = wall_ms
    results["source_unchanged"] = before == snapshot_source()
    results["before"] = before
    results["after"] = snapshot_source()
    return results


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(PROJECT_DIR))
    parser.add_argument("--only-new", action="store_true", help="run only the new-session call")
    args = parser.parse_args()

    workspace = Path(args.workspace)
    if not workspace.is_dir():
        print(f"workspace missing: {workspace}")
        return 2

    app = QApplication.instance() or QApplication([])
    session_manager = SessionManager()
    runner = QuickAskRunner(session_manager=session_manager, parent=app)
    metrics = MetricsWriter()

    telemetry_events = []
    runner.telemetry.connect(telemetry_events.append)

    # --- call 1: new native session -----------------------------------
    print(f"[smoke] call 1 (new session) workspace={workspace}")
    r1 = run_turn(app, runner, workspace, "new")
    session_id_1 = session_manager.get_native_id("claude", workspace)
    print(f"[smoke]   status tail: {r1['status'][-3:]}")
    print(f"[smoke]   text: {r1['text'][:120]!r}")
    print(f"[smoke]   exit: {r1['exit']}  failed: {r1['failed']}")
    print(f"[smoke]   source_unchanged: {r1['source_unchanged']}")
    print(f"[smoke]   session_id saved: {bool(session_id_1)}  prefix: {(session_id_1 or '')[:8]}")
    print(f"[smoke]   wall_ms: {r1['wall_ms']:.0f}")

    if not session_id_1:
        print("[smoke] FAIL: no native session id captured on first ask")
        return 1

    # --- call 2: resume the same session ------------------------------
    if not args.only_new:
        print(f"[smoke] call 2 (resume {session_id_1[:8]}...)")
        r2 = run_turn(app, runner, workspace, "resume")
        session_id_2 = session_manager.get_native_id("claude", workspace)
        print(f"[smoke]   status tail: {r2['status'][-3:]}")
        print(f"[smoke]   text: {r2['text'][:120]!r}")
        print(f"[smoke]   exit: {r2['exit']}  failed: {r2['failed']}")
        print(f"[smoke]   source_unchanged: {r2['source_unchanged']}")
        print(f"[smoke]   same session: {session_id_2 == session_id_1}")
        print(f"[smoke]   wall_ms: {r2['wall_ms']:.0f}")

    # --- telemetry + metrics files -------------------------------------
    for t in telemetry_events:
        metrics.record(t)
    print("[smoke] telemetry events:", len(telemetry_events))
    for t in telemetry_events:
        payload = t.to_payload()
        print(
            f"[smoke]   agent={payload['agent']} isolated={payload['isolated']} "
            f"first_event={payload['derived']['first_event_ms']}ms "
            f"first_text={payload['derived']['first_text_ms']}ms "
            f"total={payload['derived']['total_ms']}ms "
            f"exit={payload['process_exit']} cancel={payload['cancel_requested']}"
        )

    # --- verdict -------------------------------------------------------
    ok = True
    if r1["failed"] or r1["exit"] != 0 or "OK" not in r1["text"]:
        ok = False
        print("[smoke] FAIL: call 1 did not return OK")
    if not r1["source_unchanged"]:
        ok = False
        print("[smoke] FAIL: call 1 rewrote runtime/sources/claude.json (hook collision!)")
    if not args.only_new:
        if r2["failed"] or r2["exit"] != 0 or "OK" not in r2["text"]:
            ok = False
            print("[smoke] FAIL: call 2 did not return OK")
        if not r2["source_unchanged"]:
            ok = False
            print("[smoke] FAIL: call 2 rewrote runtime/sources/claude.json (hook collision!)")
        if session_id_2 != session_id_1:
            ok = False
            print("[smoke] FAIL: resume did not reuse the native session")
    if ok:
        print("[smoke] PASS")
        return 0
    print("[smoke] FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
