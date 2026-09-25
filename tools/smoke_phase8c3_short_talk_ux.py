"""Phase 8C.3 online smoke — exactly 2 extremely short Claude asks through Short Talk.

Drives the REAL production chain end to end: QuickAskRunner.ask(isolated=True)
-> PowerShell wrapper -> claude.CMD -> native claude.exe --safe-mode, feeding
every AgentEvent into the ShortAskPanel state machine:

  call 1 (new native session):  CONNECTING -> (thinking/generating) -> STREAMING
                                 (first TEXT_DELTA immediately visible) -> COMPLETE
  call 2 (--resume same session): same flow.

Verifies first-token-first (the output label shows the first delta in the same
signal pass), the panel reaches COMPLETE with the "OK" answer, first-text and
total timings come from telemetry, and runtime/sources/claude.json is NOT
rewritten by either internal ask. Codex online calls: 0.

Usage:
  python tools/smoke_phase8c3_short_talk_ux.py [--workspace <仓库目录>]
"""

from __future__ import annotations

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
from ui.short_ask import ShortAskPanel, ShortTalkState

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
    parser.add_argument("--only-new", action="store_true", help="run only the new-session call")
    args = parser.parse_args()
    workspace = Path(args.workspace)
    if not workspace.is_dir():
        print(f"[smoke] workspace missing: {workspace}")
        return 2

    app = QApplication.instance() or QApplication([])
    session_manager = SessionManager()
    runner = QuickAskRunner(session_manager=session_manager, parent=app)
    panel = ShortAskPanel()
    panel.show()

    holder: dict = {"cur": None}

    def on_agent_event(ev) -> None:
        panel.on_agent_event(ev)
        cur = holder["cur"]
        if cur is None:
            return
        cur["states"].append(panel.state.value)
        if ev.type == AgentEventType.TEXT_DELTA and ev.text and cur.get("first_delta") is None:
            # first-token-first: the label already reflects the delta synchronously.
            cur["first_delta"] = panel._output.text()

    def on_telemetry(t) -> None:
        panel.on_telemetry(t)
        cur = holder["cur"]
        if cur is not None:
            cur["telemetry"] = t

    runner.agent_event.connect(on_agent_event)
    runner.telemetry.connect(on_telemetry)
    before = snapshot_source()

    def run_turn(label: str, resume: bool) -> dict:
        loop = QEventLoop()
        before = snapshot_source()
        cur = {
            "label": label,
            "states": [],
            "first_delta": None,
            "text": "",
            "exit": None,
            "failed": None,
            "telemetry": None,
            "wall_ms": 0.0,
            "source_unchanged": True,
        }
        holder["cur"] = cur

        def on_finished(text, code):
            cur.update(text=text, exit=code)
            loop.quit()

        def on_failed(msg):
            cur.update(failed=msg)
            loop.quit()

        runner.finished.connect(on_finished)
        runner.failed.connect(on_failed)

        panel.show_input("claude", resume=resume)
        panel.set_running("Connecting…")
        t0 = time.time()
        ok = runner.ask("claude", PROMPT, workspace, effort="low", persistent=True, isolated=True)
        if not ok:
            cur["failed"] = cur["failed"] or "ask() returned False"
            loop.quit()
        else:
            loop.exec()
        cur["wall_ms"] = (time.time() - t0) * 1000
        panel.finish_turn(cur["text"])
        cur["source_unchanged"] = before == snapshot_source()

        runner.finished.disconnect(on_finished)
        runner.failed.disconnect(on_failed)
        return cur

    print(f"[smoke] call 1 (new session) workspace={workspace}")
    r1 = run_turn("new", resume=False)
    session_id_1 = session_manager.get_native_id("claude", workspace)
    print(f"[smoke]   states: {r1['states']}")
    print(f"[smoke]   first_delta label: {r1['first_delta']!r}")
    print(f"[smoke]   final state: {panel.state.value}  running: {panel.running}")
    print(f"[smoke]   answer: {panel.full_answer()[:80]!r}")
    print(f"[smoke]   exit: {r1['exit']}  failed: {r1['failed']}")
    print(f"[smoke]   wall_ms: {r1['wall_ms']:.0f}")

    if not session_id_1:
        print("[smoke] FAIL: no native session id captured on first ask")
        return 1

    r2 = None
    if not args.only_new:
        print(f"[smoke] call 2 (resume {session_id_1[:8]}...)")
        r2 = run_turn("resume", resume=True)
        session_id_2 = session_manager.get_native_id("claude", workspace)
        print(f"[smoke]   states: {r2['states']}")
        print(f"[smoke]   first_delta label: {r2['first_delta']!r}")
        print(f"[smoke]   final state: {panel.state.value}  running: {panel.running}")
        print(f"[smoke]   answer: {panel.full_answer()[:80]!r}")
        print(f"[smoke]   exit: {r2['exit']}  failed: {r2['failed']}")
        print(f"[smoke]   same session: {session_id_2 == session_id_1}")
        print(f"[smoke]   wall_ms: {r2['wall_ms']:.0f}")

    # timings + source isolation
    for label, r in (("call1", r1), ("call2", r2)):
        if r is None:
            continue
        t = r["telemetry"]
        if t is not None:
            print(
                f"[smoke]   {label} telemetry: first_event={t.first_event_ms()}ms "
                f"first_text={t.first_text_ms()}ms total={t.total_ms()}ms "
                f"t5={t.milestone_t5 is not None}"
            )
        else:
            print(f"[smoke]   {label} telemetry: <none>")

    after = snapshot_source()
    print(f"[smoke] claude.json present: {SOURCE_FILE.exists()}")
    print(f"[smoke] claude.json unchanged: {before == after}")

    bad = []
    for label, r in (("call1", r1), ("call2", r2)):
        if r is None:
            continue
        if r["failed"] or r["exit"] != 0 or "OK" not in (r["text"] or ""):
            bad.append(f"{label} did not return OK")
        if r["first_delta"] is None:
            bad.append(f"{label} never showed a first TEXT_DELTA")
        if ShortTalkState.CONNECTING.value not in r["states"]:
            bad.append(f"{label} never entered CONNECTING")
        if r["states"][-1] != ShortTalkState.COMPLETE.value:
            bad.append(f"{label} final state was {r['states'][-1]!r}, not complete")
        if not r["source_unchanged"]:
            bad.append(f"{label} rewrote runtime/sources/claude.json (hook collision!)")
    if not args.only_new and session_id_1 and r2 is not None:
        if session_manager.get_native_id("claude", workspace) != session_id_1:
            bad.append("resume did not reuse the native session")

    if bad:
        print("[smoke] FAIL: " + "; ".join(bad))
        return 1
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
