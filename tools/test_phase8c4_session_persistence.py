"""Phase 8C.4 — Session persistence & recovery tests.

Covers the 30 required scenarios: empty-store load, set->save, restart/reload,
Claude kind=session + Codex kind=thread preserved, workspace/agent isolation,
update replaces the right record, clear removes memory + disk, clear-one-keeps-
another, malformed top-level fallback, malformed single record skipped,
partial/missing fields skipped, version handling, unknown field tolerance,
atomic save, full session id never shown in UI text, telemetry never stores the
full id, persisted session -> resume args, New Session -> no resume, stale
resume classification, stale -> record cleared, stale -> one new-session
fallback max, network/auth errors never clear, workspace switch reads the
right session, restart restores SessionPopover state, ChatGPT writes no fake
record, internal asks stay --safe-mode, and claude.json isolation is unchanged.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import inspect
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from core.quick_ask_metrics import AskTelemetry, session_hash_prefix
from core.session_manager import (
    KIND_SESSION,
    KIND_THREAD,
    SessionManager,
    classify_stale_resume,
)
from core.session_store import STORE_VERSION, SessionStore, StoredSession
from core.workspace_manager import WorkspaceManager
from ui.process_launcher import QuickAskRunner
from ui.quick_chat_protocol import build_claude_args, build_codex_args
from ui.session_popover import SessionPopover
from ui.workspace_store import WorkspaceStore


def _valid_record(
    agent: str = "claude",
    workspace: str = "C:\\Work\\ws",
    native_id: str = "sess-1",
    kind: str = KIND_SESSION,
    **extra,
) -> dict:
    rec = {
        "agent_id": agent,
        "workspace": workspace,
        "workspace_key": f"{agent}|{workspace}",
        "kind": kind,
        "native_session_id": native_id,
        "created_at": 1000,
        "updated_at": 2000,
    }
    rec.update(extra)
    return rec


def _store_file(root: Path, name: str = "sessions.json") -> Path:
    path = root / "config" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _simulate_failed_turn(
    runner: QuickAskRunner,
    *,
    ws: Path,
    error_text: str,
    resume: bool = True,
    exit_code: int = 1,
) -> None:
    """Drive the runner's failure path as if a real process exited."""
    runner._agent = "claude"
    runner._workspace = ws
    runner._persistent = True
    runner._resume_attempted = resume
    runner._stale_cleared = False
    runner._last_protocol_error = error_text
    runner._stderr_tail = []
    runner._streamed_text = []
    runner._raw_stdout = []
    runner._stdout_buffer = ""
    runner._final_text = ""
    runner._cancelled = False
    runner._adapter = None
    runner._process = None
    runner._telemetry = AskTelemetry(
        agent="claude",
        workspace="ws",
        created_at=int(time.time() * 1000),
    )
    runner._on_finished(exit_code, 0)


# -- 1-3. load / save / reload ---------------------------------------------

def test_empty_store_load(root: Path) -> None:
    store = SessionStore(_store_file(root, "s_empty.json"))
    assert store.load() == []
    mgr = SessionManager(store=store)
    assert mgr.load() == 0
    assert mgr.get_native_id("claude", root) is None


def test_set_saves_to_disk(root: Path) -> None:
    f = _store_file(root, "s_set.json")
    mgr = SessionManager(store=SessionStore(f))
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    ref = mgr.set("claude", ws, "sess-1")
    assert ref is not None
    assert f.exists()
    records = SessionStore(f).load()
    assert len(records) == 1
    assert records[0].native_session_id == "sess-1"


def test_restart_reload_gets_session(root: Path) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    SessionManager(store=SessionStore(f)).set("claude", ws, "sess-persisted")
    fresh = SessionManager(store=SessionStore(f))
    assert fresh.load() == 1
    assert fresh.get_native_id("claude", ws) == "sess-persisted"
    assert fresh.source("claude", ws) == "persisted"
    assert fresh.has("claude", ws)


# -- 4-5. kind preserved ---------------------------------------------------

def test_claude_kind_session_preserved(root: Path) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    SessionManager(store=SessionStore(f)).set("claude", ws, "sess-k")
    fresh = SessionManager(store=SessionStore(f))
    fresh.load()
    ref = fresh.get("claude", ws)
    assert ref is not None and ref.kind == KIND_SESSION


def test_codex_kind_thread_preserved(root: Path) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    SessionManager(store=SessionStore(f)).set("codex", ws, "thr-k")
    fresh = SessionManager(store=SessionStore(f))
    fresh.load()
    ref = fresh.get("codex", ws)
    assert ref is not None and ref.kind == KIND_THREAD


# -- 6-7. isolation --------------------------------------------------------

def test_workspace_isolation(root: Path) -> None:
    f = _store_file(root)
    a = root / "a"
    b = root / "b"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", a, "sess-a")
    mgr.set("claude", b, "sess-b")
    fresh = SessionManager(store=SessionStore(f))
    fresh.load()
    assert fresh.get_native_id("claude", a) == "sess-a"
    assert fresh.get_native_id("claude", b) == "sess-b"
    assert fresh.get_native_id("claude", a) != fresh.get_native_id("claude", b)


def test_agent_isolation(root: Path) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", ws, "sess-c")
    mgr.set("codex", ws, "thr-d")
    fresh = SessionManager(store=SessionStore(f))
    fresh.load()
    assert fresh.get_native_id("claude", ws) == "sess-c"
    assert fresh.get_native_id("codex", ws) == "thr-d"


# -- 8-10. update / clear semantics ----------------------------------------

def test_update_replaces_correct_session(root: Path) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", ws, "sess-old")
    mgr.set("claude", ws, "sess-new")
    records = SessionStore(f).load()
    assert len(records) == 1
    assert records[0].native_session_id == "sess-new"


def test_clear_removes_memory_and_disk(root: Path) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", ws, "sess-x")
    assert mgr.clear("claude", ws) == 1
    assert mgr.get_native_id("claude", ws) is None
    assert SessionStore(f).load() == []


def test_clear_one_keeps_another(root: Path) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", ws, "sess-c")
    mgr.set("codex", ws, "thr-d")
    mgr.clear("claude", ws)
    assert mgr.get_native_id("codex", ws) == "thr-d"
    records = SessionStore(f).load()
    assert len(records) == 1
    assert records[0].agent_id == "codex"


# -- 11-15. corruption tolerance -------------------------------------------

def test_malformed_top_level_fallback(root: Path) -> None:
    f = _store_file(root)
    f.write_text("{ this is not json !!!", encoding="utf-8")
    assert SessionStore(f).load() == []
    # top-level wrong shape
    f.write_text("[1, 2, 3]", encoding="utf-8")
    assert SessionStore(f).load() == []
    # top-level wrong type for sessions
    f.write_text(json.dumps({"version": 1, "sessions": "nope"}), encoding="utf-8")
    assert SessionStore(f).load() == []


def test_malformed_single_record_skipped(root: Path) -> None:
    f = _store_file(root)
    payload = {
        "version": 1,
        "sessions": [
            _valid_record(native_id="good"),
            {"agent_id": "claude"},  # missing native_session_id
            "garbage",
            None,
            42,
        ],
    }
    f.write_text(json.dumps(payload), encoding="utf-8")
    records = SessionStore(f).load()
    assert len(records) == 1
    assert records[0].native_session_id == "good"


def test_partial_missing_fields_skipped(root: Path) -> None:
    f = _store_file(root)
    payload = {
        "version": 1,
        "sessions": [
            {"agent_id": "claude", "workspace": "C:\\x", "native_session_id": "ok"},
            {"agent_id": "", "workspace": "C:\\x", "native_session_id": "bad-empty-agent"},
            {"agent_id": "claude", "workspace": "C:\\x", "native_session_id": "   "},
            {"agent_id": "claude", "workspace": "", "native_session_id": "bad-empty-ws"},
        ],
    }
    f.write_text(json.dumps(payload), encoding="utf-8")
    records = SessionStore(f).load()
    assert len(records) == 1
    assert records[0].native_session_id == "ok"


def test_version_handling(root: Path) -> None:
    f = _store_file(root)
    f.write_text(
        json.dumps({"version": 99, "sessions": [_valid_record()]}), encoding="utf-8"
    )
    assert SessionStore(f).load() == []  # future schema is not guessed
    f.write_text(json.dumps({"sessions": [_valid_record()]}), encoding="utf-8")
    assert len(SessionStore(f).load()) == 1  # missing version tolerated
    f.write_text(
        json.dumps({"version": STORE_VERSION, "sessions": [_valid_record()]}),
        encoding="utf-8",
    )
    assert len(SessionStore(f).load()) == 1


def test_unknown_field_tolerance(root: Path) -> None:
    f = _store_file(root)
    rec = _valid_record(native_id="sess-future")
    rec["some_future_field"] = {"nested": [1, 2]}
    rec["other"] = "x"
    f.write_text(json.dumps({"version": 1, "sessions": [rec]}), encoding="utf-8")
    records = SessionStore(f).load()
    assert len(records) == 1
    assert records[0].native_session_id == "sess-future"


# -- 16. atomic save -------------------------------------------------------

def test_atomic_save(root: Path) -> None:
    f = _store_file(root)
    store = SessionStore(f)
    store.save([StoredSession("claude", str(root / "ws"), KIND_SESSION, "sess-a", 1, 2)])
    assert f.exists()
    assert not f.with_name(f.name + ".tmp").exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["version"] == STORE_VERSION
    assert data["sessions"][0]["native_session_id"] == "sess-a"
    # a leftover .tmp from a crashed writer must not confuse reads
    f.with_name(f.name + ".tmp").write_text("{broken", encoding="utf-8")
    assert len(SessionStore(f).load()) == 1


def test_prune_cap_keeps_newest(root: Path) -> None:
    f = _store_file(root)
    store = SessionStore(f, max_records=2)
    store.save(
        [
            StoredSession("claude", str(root / f"w{i}"), KIND_SESSION, f"s{i}", i, i * 100)
            for i in range(3)
        ]
    )
    records = store.load()
    assert len(records) == 2
    assert {r.native_session_id for r in records} == {"s1", "s2"}


# -- 17. UI never exposes the full id --------------------------------------

def test_session_id_not_exposed_to_ui(root: Path, app: QApplication) -> None:
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    store = WorkspaceStore(root / "config" / "ui_settings.json", ws)
    wm = WorkspaceManager(store)
    sm = SessionManager()
    secret = "native-session-secret-full-id-12345"
    sm.set("claude", ws, secret)
    popover = SessionPopover(sm, wm)
    popover.show_at(QPoint(0, 0))
    app.processEvents()
    label = popover._sections["claude"]._status_label
    exposed = popover.agent_session_text("claude") + " " + (label.toolTip() or "")
    assert secret not in exposed
    assert session_hash_prefix(secret) in exposed  # hash prefix is the hint
    popover.dismiss()
    app.processEvents()


# -- 18. telemetry never stores the full id --------------------------------

def test_telemetry_does_not_store_full_id() -> None:
    secret = "native-session-secret-telemetry-98765"
    t = AskTelemetry(
        agent="claude",
        workspace="firefly",
        created_at=1,
        session_exists=True,
        session_hash=session_hash_prefix(secret),
        session_source="persisted",
        resume_attempted=True,
    )
    payload_text = json.dumps(t.to_payload())
    assert secret not in payload_text
    assert t.session_hash is not None and len(t.session_hash) <= 12
    assert t.validate_safe() == []
    assert payload_text.count("resume_fallback") == 1


# -- 19-20. resume args ----------------------------------------------------

def test_persisted_session_builds_resume_args(root: Path, app: QApplication) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    SessionManager(store=SessionStore(f)).set("claude", ws, "sess-resume-args")
    fresh = SessionManager(store=SessionStore(f))
    fresh.load()
    sid = fresh.get_native_id("claude", ws)
    assert sid == "sess-resume-args"
    claude_args = build_claude_args("again", session_id=sid, persistent=True, isolated=True)
    assert "--resume" in claude_args and sid in claude_args
    runner = QuickAskRunner(session_manager=fresh, parent=app)
    assert runner.session_id("claude", ws) == sid
    codex_args = build_codex_args("again", ws, session_id=sid, persistent=True)
    assert codex_args[-3:] == ["resume", sid, "again"]


def test_new_session_no_resume(root: Path, app: QApplication) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", ws, "sess-x")
    mgr.clear("claude", ws)
    args = build_claude_args(
        "hi",
        session_id=mgr.get_native_id("claude", ws),
        persistent=True,
        isolated=True,
    )
    assert "--resume" not in args
    assert SessionStore(f).load() == []
    # a fresh manager (no session at all) also never resumes
    args2 = build_claude_args("hi", session_id=None, persistent=True, isolated=True)
    assert "--resume" not in args2


# -- 21-25. stale / network / auth handling --------------------------------

def test_stale_classification() -> None:
    stale = [
        "Session not found: abc123",
        "Error: could not find session 'abc'",
        "Unable to find session with id abc",
        "No session with id abc found",
        "invalid session id abc",
        "the session no longer exists",
        "Session expired",
        # verified against claude.exe 2.1.233 --print on an invalid --resume:
        "--resume requires a valid session ID or session title when used with --print.",
        "Provided value \"fake-id\" does not match any session title.",
        # the real combined stderr seen in the end-to-end probe
        "Error: --resume requires a valid session ID or session title when used with "
        "--print. Usage: claude -p --resume <session-id|title>. Provided value "
        "\"definitely-not-a-real-session-8c4-xyz\" is not a UUID and does not match "
        "any session title.",
    ]
    for text in stale:
        assert classify_stale_resume(text), f"{text!r} should be stale"
    assert not classify_stale_resume("Request timed out after 30 seconds")
    assert not classify_stale_resume("Connection to claude.ai failed (proxy)")
    assert not classify_stale_resume("Authentication failed: invalid API key")
    assert not classify_stale_resume("network error while resuming")
    assert not classify_stale_resume(None)
    assert not classify_stale_resume("")


def test_stale_clears_record(root: Path, app: QApplication) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", ws, "sess-stale")
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    _simulate_failed_turn(
        runner, ws=ws, error_text="Session not found: sess-stale", resume=True
    )
    assert runner.stale_cleared is True
    assert runner.resume_attempted is True
    assert mgr.get_native_id("claude", ws) is None
    assert SessionStore(f).load() == []
    assert runner._telemetry.resume_fallback is True


def test_network_error_does_not_clear(root: Path, app: QApplication) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", ws, "sess-net")
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    _simulate_failed_turn(
        runner, ws=ws, error_text="Connection timed out after 30s", resume=True
    )
    assert runner.stale_cleared is False
    assert mgr.get_native_id("claude", ws) == "sess-net"
    assert len(SessionStore(f).load()) == 1


def test_auth_error_does_not_clear(root: Path, app: QApplication) -> None:
    f = _store_file(root)
    ws = root / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", ws, "sess-auth")
    runner = QuickAskRunner(session_manager=mgr, parent=app)
    _simulate_failed_turn(
        runner, ws=ws, error_text="Authentication failed: invalid credentials", resume=True
    )
    assert runner.stale_cleared is False
    assert mgr.get_native_id("claude", ws) == "sess-auth"
    assert len(SessionStore(f).load()) == 1


# -- 26. workspace switch --------------------------------------------------

def test_workspace_switch_reads_different_session(root: Path, app: QApplication) -> None:
    f = _store_file(root)
    a = root / "a"
    b = root / "b"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    mgr = SessionManager(store=SessionStore(f))
    mgr.set("claude", a, "sess-a")
    mgr.set("claude", b, "sess-b")
    fresh = SessionManager(store=SessionStore(f))
    fresh.load()
    assert fresh.get("claude", a).native_session_id == "sess-a"
    assert fresh.get("claude", b).native_session_id == "sess-b"
    refs = fresh.for_workspace(b)
    assert list(refs) == ["claude"]
    assert refs["claude"].native_session_id == "sess-b"


# -- 27. restart restores SessionPopover -----------------------------------

def test_restart_restores_popover(root: Path, app: QApplication) -> None:
    from app import VisualShell

    f = _store_file(root)
    shell1 = VisualShell(None, sessions_file=f)
    ws = shell1.workspace_manager.current()
    shell1.session_manager.set("claude", ws, "sess-popover-restore")
    shell1.shutdown()

    shell2 = VisualShell(None, sessions_file=f)
    try:
        assert shell2.session_manager.load() == 1
        assert shell2.session_manager.has("claude", ws)
        shell2.session_popover.show_at(QPoint(0, 0))
        app.processEvents()
        assert shell2.session_popover.agent_session_text("claude") == "Current session"
        assert shell2.session_popover.agent_has_continue("claude") is True
        # other agents unaffected
        assert shell2.session_popover.agent_session_text("codex") == "No active session"
        shell2.session_popover.dismiss()
        app.processEvents()
    finally:
        shell2.shutdown()


# -- 28. ChatGPT writes no fake record -------------------------------------

def test_chatgpt_no_fake_record(root: Path, app: QApplication) -> None:
    from app import VisualShell

    f = _store_file(root)
    shell = VisualShell(None, sessions_file=f)
    try:
        shell.short_ask.reset()
        with patch.object(shell.quick_ask, "ask") as ask_mock:
            shell.dock.select_agent("chatgpt", emit_signal=True)
            shell._on_short_ask_requested()
            assert ask_mock.call_count == 0
        assert shell.session_manager.has("chatgpt", shell.workspace_manager.current()) is False
        assert shell.session_manager.get("chatgpt", shell.workspace_manager.current()) is None
        assert SessionStore(f).load() == []
    finally:
        shell.shutdown()


# -- 29-30. internal hook isolation unchanged ------------------------------

def test_internal_ask_still_safe_mode(root: Path, app: QApplication) -> None:
    from app import VisualShell

    shell = VisualShell(None, sessions_file=_store_file(root))
    try:
        shell.short_ask.reset()
        with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
            shell.dock.select_agent("claude", emit_signal=True)
            shell._on_short_ask_requested()
            shell._on_short_ask_send("hello")
            assert ask_mock.call_count == 1
            kwargs = ask_mock.call_args.kwargs
            assert kwargs.get("isolated") is True
            assert kwargs.get("persistent") is True
        assert "--safe-mode" in build_claude_args("hi", isolated=True)
        assert "--safe-mode" not in build_claude_args("hi", isolated=False)
    finally:
        shell.shutdown()


def test_claude_json_isolation_mechanism() -> None:
    # --safe-mode disables lifecycle hooks for internal asks, so
    # runtime/sources/claude.json stays untouched by the resume path.
    args = build_claude_args("hi", session_id="sess-x", persistent=True, isolated=True)
    assert "--safe-mode" in args
    assert "--resume" in args and "sess-x" in args


# -- bonus: one new-session fallback max ------------------------------------

def test_stale_one_fallback_max(root: Path, app: QApplication) -> None:
    from app import VisualShell

    shell = VisualShell(None, sessions_file=_store_file(root))
    try:
        shell.short_ask.reset()
        shell._resume_fallbacks_this_cycle = 0
        shell._last_short_ask_prompt = "please retry me"
        shell.dock.select_agent("claude", emit_signal=True)
        with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
            # first stale failure -> one auto-fallback with a new session
            shell.quick_ask._stale_cleared = True
            shell._on_short_ask_failed("Session not found: xyz")
            assert ask_mock.call_count == 1
            assert shell.short_ask._status.text() == (
                "Previous session expired. Starting a new session…"
            )
            # a second stale failure must NOT fall back again
            shell.quick_ask._stale_cleared = True
            shell._on_short_ask_failed("Session not found: xyz")
            assert ask_mock.call_count == 1
            assert shell._resume_fallbacks_this_cycle == 1
    finally:
        shell.shutdown()


def _run_in_fresh_dir(fn, app: QApplication) -> None:
    """Run each test in its own temp dir so store/workspace state never
    leaks between scenarios."""
    params = list(inspect.signature(fn).parameters)
    if not params:
        fn()
    elif "app" in params:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td), app)
    else:
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td))


def main() -> None:
    app = QApplication.instance() or QApplication([])
    tests = [
        test_empty_store_load,
        test_set_saves_to_disk,
        test_restart_reload_gets_session,
        test_claude_kind_session_preserved,
        test_codex_kind_thread_preserved,
        test_workspace_isolation,
        test_agent_isolation,
        test_update_replaces_correct_session,
        test_clear_removes_memory_and_disk,
        test_clear_one_keeps_another,
        test_malformed_top_level_fallback,
        test_malformed_single_record_skipped,
        test_partial_missing_fields_skipped,
        test_version_handling,
        test_unknown_field_tolerance,
        test_atomic_save,
        test_prune_cap_keeps_newest,
        test_session_id_not_exposed_to_ui,
        test_telemetry_does_not_store_full_id,
        test_persisted_session_builds_resume_args,
        test_new_session_no_resume,
        test_stale_classification,
        test_stale_clears_record,
        test_network_error_does_not_clear,
        test_auth_error_does_not_clear,
        test_workspace_switch_reads_different_session,
        test_restart_restores_popover,
        test_chatgpt_no_fake_record,
        test_internal_ask_still_safe_mode,
        test_claude_json_isolation_mechanism,
        test_stale_one_fallback_max,
    ]
    for fn in tests:
        _run_in_fresh_dir(fn, app)

    print("Phase 8C.4 session persistence tests passed.")


if __name__ == "__main__":
    main()
