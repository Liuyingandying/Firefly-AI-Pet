"""Phase 8C.4 online smoke — stale fallback + cross-restart session recovery.

Drives the REAL production chain end to end (QuickAskRunner.ask(isolated=True)
-> PowerShell wrapper -> claude.CMD -> claude.exe --safe-mode). Online budget
is 3 short invocations, of which only 2 reach the model:

  1. stale probe  — an obviously-invalid synthetic resume id. The CLI rejects
     it before any model call (num_turns 0, cost 0), and the runner must
     classify it stale, clear memory + disk, and leave no phantom id behind.
  2. new session  — Reply exactly: SESSION-A; native id persisted.
  3. resume       — recreate SessionManager from the same store file (simulates
     a Firefly restart), then Reply exactly: SESSION-B through --resume.

Verifies: persisted session -> --resume really used, session continuity holds,
the on-disk store round-trips, and runtime/sources/claude.json is NOT rewritten
by any internal ask. Codex online calls: 0.

Usage:
  python tools/smoke_phase8c4_session_persistence.py [--workspace <仓库目录>]
  python tools/smoke_phase8c4_session_persistence.py --only-continuity
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QApplication

from core.session_manager import SessionManager
from core.session_store import SessionStore
from ui.process_launcher import QuickAskRunner

PROMPT_A = "Reply exactly: SESSION-A"
PROMPT_B = "Reply exactly: SESSION-B"
SOURCE_FILE = PROJECT_DIR / "runtime" / "sources" / "claude.json"
FAKE_ID = "definitely-not-a-real-session-8c4-smoke"


def snapshot_source() -> str:
    try:
        return SOURCE_FILE.read_text(encoding="utf-8")
    except OSError:
        return "<missing>"


def run_turn(runner: QuickAskRunner, prompt: str, workspace: Path) -> dict:
    loop = QEventLoop()
    cur = {
        "text": "",
        "exit": None,
        "failed": None,
        "telemetry": None,
        "wall_ms": 0.0,
    }

    def on_finished(text, code):
        cur.update(text=text, exit=code)
        loop.quit()

    def on_failed(msg):
        cur.update(failed=msg)
        loop.quit()

    runner.finished.connect(on_finished)
    runner.failed.connect(on_failed)
    runner.telemetry.connect(lambda t: cur.update(telemetry=t))
    t0 = time.time()
    ok = runner.ask("claude", prompt, workspace, effort="low", persistent=True, isolated=True)
    if not ok:
        cur["failed"] = cur["failed"] or "ask() returned False"
        loop.quit()
    else:
        loop.exec()
    cur["wall_ms"] = (time.time() - t0) * 1000
    runner.finished.disconnect(on_finished)
    runner.failed.disconnect(on_failed)
    return cur


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(PROJECT_DIR))
    parser.add_argument(
        "--only-continuity",
        action="store_true",
        help="skip the zero-token stale probe and run only new -> resume",
    )
    args = parser.parse_args()
    workspace = Path(args.workspace)
    if not workspace.is_dir():
        print(f"[smoke] workspace missing: {workspace}")
        return 2

    app = QApplication.instance() or QApplication([])
    before = snapshot_source()
    failures: list[str] = []

    tmp = Path(tempfile.mkdtemp(prefix="firefly_8c4_smoke_"))
    stale_file = tmp / "sessions_stale.json"
    cont_file = tmp / "sessions_continuity.json"

    # -- 1. stale probe (zero model tokens) -----------------------------
    if not args.only_continuity:
        print("[smoke] 1) stale probe: resume an invalid id")
        stale_mgr = SessionManager(store=SessionStore(stale_file))
        stale_mgr.set("claude", workspace, FAKE_ID)
        stale_runner = QuickAskRunner(session_manager=stale_mgr, parent=app)
        r = run_turn(stale_runner, PROMPT_A, workspace)
        print(f"[smoke]    resume_attempted={stale_runner.resume_attempted} "
              f"stale_cleared={stale_runner.stale_cleared} exit={r['exit']} failed={bool(r['failed'])}")
        after_stale = stale_mgr.get_native_id("claude", workspace)
        print(f"[smoke]    session after stale: {after_stale!r}")
        print(f"[smoke]    store records after stale: {len(SessionStore(stale_file).load())}")
        if not stale_runner.stale_cleared:
            failures.append("stale probe did not classify the invalid resume as stale")
        if after_stale is not None:
            failures.append(f"stale probe left a session behind: {after_stale!r}")
        if SessionStore(stale_file).load():
            failures.append("stale probe left a persisted record")
        if r["telemetry"] is None or not r["telemetry"].resume_fallback:
            failures.append("stale probe telemetry.resume_fallback not set")
    else:
        print("[smoke] 1) stale probe skipped (--only-continuity)")

    # -- 2. new session -> persist ---------------------------------------
    print("[smoke] 2) new session (SESSION-A)")
    mgr_a = SessionManager(store=SessionStore(cont_file))
    runner_a = QuickAskRunner(session_manager=mgr_a, parent=app)
    ra = run_turn(runner_a, PROMPT_A, workspace)
    sid_a = mgr_a.get_native_id("claude", workspace)
    print(f"[smoke]    exit={ra['exit']} failed={bool(ra['failed'])} answer={ra['text'][:60]!r}")
    print(f"[smoke]    resume_attempted={runner_a.resume_attempted} "
          f"session_source={ra['telemetry'].session_source if ra['telemetry'] else None}")
    print(f"[smoke]    session id: {sid_a[:8] if sid_a else None}...  wall={ra['wall_ms']:.0f}ms")
    if not sid_a:
        failures.append("call 2 did not produce a native session id")
    if ra["failed"] or ra["exit"] != 0 or "SESSION-A" not in (ra["text"] or ""):
        failures.append("call 2 did not return SESSION-A")
    persisted = SessionStore(cont_file).load()
    if len(persisted) != 1 or persisted[0].native_session_id != sid_a:
        failures.append("call 2 did not persist exactly one session record")

    # -- 3. simulate Firefly restart: fresh manager, load, resume --------
    print("[smoke] 3) restart SessionManager -> resume (SESSION-B)")
    mgr_b = SessionManager(store=SessionStore(cont_file))
    restored = mgr_b.load()
    sid_b_before = mgr_b.get_native_id("claude", workspace)
    print(f"[smoke]    load restored {restored} record(s), source={mgr_b.source('claude', workspace)}")
    if sid_b_before != sid_a:
        failures.append("restart did not restore the persisted session id")

    runner_b = QuickAskRunner(session_manager=mgr_b, parent=app)
    rb = run_turn(runner_b, PROMPT_B, workspace)
    sid_b_after = mgr_b.get_native_id("claude", workspace)
    print(f"[smoke]    exit={rb['exit']} failed={bool(rb['failed'])} answer={rb['text'][:60]!r}")
    print(f"[smoke]    resume_attempted={runner_b.resume_attempted} "
          f"session_source={rb['telemetry'].session_source if rb['telemetry'] else None}")
    print(f"[smoke]    session id after resume: {sid_b_after[:8] if sid_b_after else None}...  wall={rb['wall_ms']:.0f}ms")
    if not runner_b.resume_attempted:
        failures.append("call 3 did not attempt --resume (no persisted id used)")
    if rb["failed"] or rb["exit"] != 0 or "SESSION-B" not in (rb["text"] or ""):
        failures.append("call 3 did not return SESSION-B")
    if sid_b_after != sid_a:
        failures.append(f"resume changed session id: {sid_a} -> {sid_b_after}")
    if mgr_b.source("claude", workspace) != "persisted":
        failures.append("resumed session provenance is not 'persisted'")

    # -- isolation -------------------------------------------------------
    after = snapshot_source()
    print(f"[smoke] claude.json present: {SOURCE_FILE.exists()}")
    print(f"[smoke] claude.json unchanged: {before == after}")
    if before != after:
        failures.append("an internal ask rewrote runtime/sources/claude.json")

    for label, t in (("call2", ra), ("call3", rb)):
        if t["telemetry"] is not None:
            print(
                f"[smoke]   {label} telemetry: first_event={t['telemetry'].first_event_ms()}ms "
                f"first_text={t['telemetry'].first_text_ms()}ms total={t['telemetry'].total_ms()}ms"
            )

    if failures:
        print("[smoke] FAIL: " + "; ".join(failures))
        return 1
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
