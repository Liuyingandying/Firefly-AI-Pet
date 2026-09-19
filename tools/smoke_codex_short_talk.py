"""Codex Short Talk — real smoke (feature/codex-short-talk).

Two real Codex exec calls through the production QuickAskRunner (read-only,
ephemeral, no workspace writes):
  call 1 (short): "晚上好，请用一句中文回复。"  -> exit code, AgentEvent sequence,
                  text received, final.
  call 2 (long):  "请用约 500 字介绍 Python。" -> ShortAskPanel scrolling + Done.

Proves the workspace is unchanged (git status --porcelain before/after) and that
Codex Short Talk reuses the shared ScrollFollowTextBrowser (full text + scroll).

Usage:
  python tools/smoke_codex_short_talk.py [--workspace <仓库目录>]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from core.agent_events import AgentEventType
from core.session_manager import SessionManager
from ui.process_launcher import QuickAskRunner
from ui.short_ask import ShortAskPanel, ShortTalkState


def git_porcelain(workspace: Path) -> str:
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return r.stdout


def _redact(s: str) -> str:
    s = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "<redacted>", s)
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<redacted-uuid>", s)
    return s


def _run_turn(app: QApplication, runner: QuickAskRunner, workspace: Path, prompt: str) -> dict:
    events: list[tuple[str, str | None]] = []
    failed: list[str] = []
    finished: list[tuple[str, int]] = []
    runner.agent_event.connect(lambda ev: events.append((ev.type.value, ev.error_code)))
    runner.failed.connect(failed.append)
    runner.finished.connect(lambda t, c: finished.append((t, c)))

    loop = QEventLoop()
    runner.finished.connect(lambda *a: loop.quit())
    runner.failed.connect(lambda *a: loop.quit())

    ok = runner.ask("codex", prompt, workspace, effort="low", persistent=False, isolated=True)
    if ok:
        QTimer.singleShot(180_000, loop.quit)
        loop.exec()

    return {
        "ok": ok,
        "events": [e[0] for e in events],
        "failed": failed,
        "exit": finished[0][1] if finished else None,
        "text": finished[0][0] if finished else "",
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(PROJECT_DIR))
    args = parser.parse_args()
    workspace = Path(args.workspace)
    if not workspace.is_dir():
        print(f"[smoke] workspace missing: {workspace}")
        return 2

    app = QApplication.instance() or QApplication([])
    before = git_porcelain(workspace)

    # -- call 1: short ----------------------------------------------------
    print(f"[smoke] call 1 (short) workspace={workspace}")
    runner = QuickAskRunner(session_manager=SessionManager(), parent=app)
    r1 = _run_turn(app, runner, workspace, "晚上好，请用一句中文回复。")
    print(f"[smoke]   ok={r1['ok']} exit={r1['exit']} failed={bool(r1['failed'])}")
    print(f"[smoke]   events: {r1['events']}")
    print(f"[smoke]   text: {_redact(r1['text'][:120])!r}")

    # -- call 2: long, through the panel ----------------------------------
    print("[smoke] call 2 (long, panel)")
    panel = ShortAskPanel()
    panel.show_input("codex")
    panel.show()
    runner2 = QuickAskRunner(session_manager=SessionManager(), parent=app)
    states: list[str] = []
    runner2.agent_event.connect(lambda ev: (panel.on_agent_event(ev), states.append(panel.state.value)))
    loop2 = QEventLoop()
    runner2.finished.connect(lambda *a: loop2.quit())
    runner2.failed.connect(lambda *a: loop2.quit())
    panel.set_running("Connecting…")
    ok2 = runner2.ask("codex", "请用约 500 字介绍 Python。", workspace, effort="low", persistent=False, isolated=True)
    if ok2:
        QTimer.singleShot(240_000, loop2.quit)
        loop2.exec()
    app.processEvents()
    bar = panel._output.verticalScrollBar()
    print(f"[smoke]   final state: {panel.state.value}  running={panel.running}")
    print(f"[smoke]   answer len: {len(panel.full_answer())}")
    print(f"[smoke]   scrollbar: max={bar.maximum()} value={bar.value()} follow_tail={panel._output.follow_tail}")
    print(f"[smoke]   Done shown: {panel._status.text().startswith('Done')}")

    after = git_porcelain(workspace)
    print(f"[smoke] workspace changed: {before != after}")
    if before != after:
        print("[smoke]   git diff:")
        for line in after.splitlines():
            print(f"[smoke]     {line}")

    panel.close()
    runner.shutdown()
    runner2.shutdown()

    bad = []
    if not r1["ok"] or r1["exit"] != 0 or not r1["text"]:
        bad.append("call 1 did not return a non-empty answer with exit 0")
    if AgentEventType.FINAL.value not in r1["events"]:
        bad.append("call 1 never emitted FINAL")
    if not ok2 or panel.state != ShortTalkState.COMPLETE:
        bad.append("call 2 did not reach COMPLETE")
    if len(panel.full_answer()) < 100:
        bad.append("call 2 answer is unexpectedly short")
    if before != after:
        bad.append("workspace changed (read-only Codex Short Talk modified files)")

    if bad:
        print("[smoke] FAIL: " + "; ".join(bad))
        return 1
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
