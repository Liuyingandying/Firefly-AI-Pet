"""Phase 9C — User-Confirmed Native Handoff tests.

Covers the 36 required scenarios plus the shell-injection hard gate (section
35): a recommendation never auto-executes; only ``Send to <agent>`` creates a
HandoffRequest; Open only (secondary and light) never creates a native handoff;
the original prompt reaches the native Agent as ONE argv item through the real
PowerShell ``-File`` wrapper (no shell string, no interpolation); quotes /
newlines / ``;`` / ``&`` / ``|`` / ``$()`` / backticks / Unicode / emoji /
Windows paths are preserved verbatim; ``-``-prefixed and subcommand-shaped
prompts are refused; the workspace and agent stay locked; workspace drift
cancels the pending handoff; double-click launches only once; HANDED_OFF never
pretends to be task success; launch failure is FAILED and never writes the
external lifecycle; no managed SessionManager session is created; PermissionCard
is never auto-approved; ChatGPT/vision never fabricate a send; Claude Short Talk
and the Codex route do not regress; the router stays zero-LLM; the UI never
shows internal scores/reason tokens; telemetry never contains the prompt; Open
only keeps full 9B compatibility; and protected lifecycle files are untouched.

No online model calls: every launch is patched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.handoff import (
    HandoffMetrics,
    HandoffState,
    HandoffTelemetry,
    make_handoff_request,
    validate_handoff_prompt,
    workspace_token,
)
from ui.process_launcher import (
    HANDOFF_TRANSPORT_VERIFIED,
    ProcessLauncher,
    find_executable,
)

MALICIOUS_SAMPLES = [
    'hello "world"',
    "hello 'world'",
    "line1\nline2",
    '; Write-Output HACKED; #',
    "$(Write-Output HACKED)",
    "& calc.exe",
    "| Remove-Item test",
    "`Write-Output HACKED`",
    "x && y || z",
    "重构这个模块 中文测试",
    "emoji 🎉✨",
    r"C:\Users\<用户名>\Some Path\file.py",
]


def _prepare(shell) -> None:
    shell.short_ask.reset()
    shell.recommendation_card.clear_pending()
    shell.dock.select_agent("claude", emit_signal=False)
    shell.coordinator._waiting.clear()
    if shell.permission_card is not None:
        shell.permission_card.hide_card()


def _open_input(shell) -> None:
    shell.dock.select_agent("claude", emit_signal=True)
    shell._on_short_ask_requested()


def _snapshot_sources() -> dict[str, str]:
    """Read every runtime/sources + runtime/state.json file (lifecycle truth)."""
    out: dict[str, str] = {}
    sources = PROJECT_DIR / "runtime" / "sources"
    if sources.exists():
        for p in sorted(sources.glob("*.json")):
            out[str(p.relative_to(PROJECT_DIR))] = p.read_text(encoding="utf-8")
    state_file = PROJECT_DIR / "runtime" / "state.json"
    if state_file.exists():
        out["runtime/state.json"] = state_file.read_text(encoding="utf-8")
    return out


# -- 1-4. user confirmation is the only execution gate ----------------------

def test_recommendation_does_not_auto_execute(shell) -> None:
    with patch.object(shell.quick_ask, "ask") as ask_mock, patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert shell.recommendation_card.has_pending
        assert launch_mock.call_count == 0 and ask_mock.call_count == 0
        assert shell.recommendation_card.handoff is None


def test_send_click_creates_handoff(shell) -> None:
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert shell.recommendation_card.handoff is None
        shell.recommendation_card._primary_btn.click()
        req = shell.recommendation_card.handoff
        assert req is not None, "Send to Codex must create a HandoffRequest"
        assert req.agent_id == "codex"
        assert req.text == "重构这个模块"
        assert req.requires_confirmation


def test_open_only_creates_no_handoff(shell) -> None:
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._light_btn.click()  # 9D.3: light = Open only
        assert shell.recommendation_card.handoff is None, (
            "Open only must not create a HandoffRequest"
        )


def test_light_open_only_creates_no_handoff(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock, patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._light_btn.click()
        assert shell.recommendation_card.handoff is None
        assert launch_mock.call_count == 1, "light Open only opens the native surface"
        assert "initial_prompt" not in launch_mock.call_args.kwargs, (
            "light Open only must not hand a task"
        )
        assert ask_mock.call_count == 0, "light Open only must not run a managed ask"


# -- 5-7. prompt fidelity + agent/workspace lock ----------------------------

def test_prompt_passed_verbatim_to_codex(shell) -> None:
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("把这个模块重构一下")
        shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_args.kwargs.get("initial_prompt") == "把这个模块重构一下"


def test_agent_locked(shell) -> None:
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.dock.select_agent("claude", emit_signal=True)
        shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_args[0][0] == "codex", "handoff must use the locked agent"


def test_workspace_locked(shell) -> None:
    locked = shell.workspace_manager.current()
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_args[0][1] == str(locked)


def test_workspace_change_invalidates_pending(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        assert shell.recommendation_card.has_pending
        with tempfile.TemporaryDirectory() as td:
            shell.workspace_manager.set_current(Path(td))
        assert not shell.recommendation_card.has_pending
        assert not shell.recommendation_card.isVisible()


def test_workspace_drift_cancels_send(shell) -> None:
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        with patch.object(shell.workspace_manager, "current", return_value=Path("C:/Other")):
            shell.recommendation_card._primary_btn.click()
        assert launch_mock.call_count == 0, "drift must cancel, not deliver to wrong workspace"
        assert shell.recommendation_card.handoff_state.value == "failed"


# -- 9-13. launch semantics ------------------------------------------------

def test_double_click_launches_once(shell) -> None:
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ) as launch_mock:
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        primary = shell.recommendation_card._primary_btn
        primary.click()
        primary.click()  # programmatic re-click must be a no-op after send
        assert launch_mock.call_count == 1, "double click must launch only once"


def test_launching_disables_primary() -> None:
    from core.routing_models import (
        AgentRecommendation,
        Confidence,
        HandoffMode,
        ReasonCode,
        TaskIntent,
    )
    from ui.recommendation_card import RecommendationCard

    card = RecommendationCard()
    rec = AgentRecommendation(
        agent_id="codex",
        score=90,
        reason_code=ReasonCode.BEST_FOR_CODING,
        confidence=Confidence.HIGH,
        intent=TaskIntent.CODE,
        handoff_mode=HandoffMode.OPEN_NATIVE,
        requires_confirmation=True,
    )
    card.show_recommendation(rec, "重构这个模块", "E:/Work")
    primary = card._primary_btn
    primary.click()  # send_requested has no connected slot -> stays LAUNCHING
    assert card.handoff_state == HandoffState.LAUNCHING
    assert not primary.isEnabled(), "primary must disable while LAUNCHING"
    card.clear_pending()


def test_spawn_success_handed_off(shell) -> None:
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert shell.recommendation_card.handoff_state == HandoffState.HANDED_OFF
        assert shell.recommendation_card._status.text() == "Sent to Codex"


def test_handed_off_not_task_success(shell) -> None:
    before = _snapshot_sources()
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert shell.recommendation_card.handoff_state == HandoffState.HANDED_OFF
        status = shell.recommendation_card._status.text().lower()
        for token in ("completed", "success", "done", "完成"):
            assert token not in status, f"HANDED_OFF must not claim task success: {token}"
    assert _snapshot_sources() == before, "handoff must not write the external lifecycle"


def test_spawn_failure_failed(shell) -> None:
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(False, "无法启动 Codex。")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert shell.recommendation_card.handoff_state == HandoffState.FAILED
        assert shell.recommendation_card._status.text() == "Couldn't start Codex"
        assert shell.recommendation_card._primary_btn.isEnabled(), "retry stays available"


# -- 14-17. lifecycle / permission isolation ---------------------------------

def test_launch_failure_no_external_lifecycle(shell) -> None:
    before = _snapshot_sources()
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(False, "无法启动 Codex。")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert shell.recommendation_card.handoff_state == HandoffState.FAILED
    assert _snapshot_sources() == before, "launch failure must not write lifecycle files"


def test_no_managed_session_created(shell) -> None:
    ws = shell.workspace_manager.current()
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert not shell.session_manager.has("codex", ws)
        assert not shell.session_manager.has("claude", ws)


def test_external_lifecycle_semantics_untouched(shell) -> None:
    """Handoff must not fabricate runtime/sources/*.json or AgentEvents."""
    before = _snapshot_sources()
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
    assert _snapshot_sources() == before, "handoff must never fabricate lifecycle state"


def test_permission_card_not_auto_approved(shell) -> None:
    card = shell.permission_card
    texts = [b.text() for b in card.findChildren(type(card._view_btn)) if b.text()]
    for forbidden in ("Approve", "Allow", "Always allow"):
        assert forbidden not in texts, f"PermissionCard must not gain {forbidden}"
    assert card._view_btn.text() == "View", "observer-only card stays observer-only"
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        assert not card.isVisible(), "handoff must never pop the permission card"


# -- 18-27. argv boundary / shell-injection hard gate -----------------------

FAKE_NATIVE = (r"C:\node\node.exe", [r"C:\codex.js"])  # (program, extra) stand-in


def _handoff_argv(prompt: str) -> list[str]:
    with patch.object(ProcessLauncher, "_resolve_native", return_value=FAKE_NATIVE):
        _program, argv = ProcessLauncher._handoff_command("codex", r"C:\fake\codex.CMD", prompt)
    return argv


def test_quote_prompt_safe() -> None:
    argv = _handoff_argv('hello "world"')
    assert argv[-1] == 'hello "world"'
    assert argv.count('hello "world"') == 1


def test_newline_prompt_safe() -> None:
    prompt = "line1\nline2\nline3"
    argv = _handoff_argv(prompt)
    assert argv[-1] == prompt and argv.count(prompt) == 1


def test_semicolon_prompt_safe() -> None:
    prompt = "; Write-Output HACKED; #"
    assert validate_handoff_prompt("codex", prompt) is None
    assert _handoff_argv(prompt)[-1] == prompt


def test_ampersand_prompt_safe() -> None:
    prompt = "& calc.exe"
    assert validate_handoff_prompt("codex", prompt) is None
    assert _handoff_argv(prompt)[-1] == prompt


def test_pipe_prompt_safe() -> None:
    prompt = "| Remove-Item test"
    assert validate_handoff_prompt("codex", prompt) is None
    assert _handoff_argv(prompt)[-1] == prompt


def test_powershell_expression_stays_text() -> None:
    prompt = "$(Write-Output HACKED)"
    assert validate_handoff_prompt("codex", prompt) is None
    argv = _handoff_argv(prompt)
    assert argv[-1] == prompt, "must stay literal task text, never evaluate"


def test_backtick_prompt_safe() -> None:
    prompt = "`Write-Output HACKED`"
    assert validate_handoff_prompt("codex", prompt) is None
    assert _handoff_argv(prompt)[-1] == prompt


def test_unicode_prompt_preserved() -> None:
    prompt = "重构这个模块 中文测试"
    assert _handoff_argv(prompt)[-1] == prompt


def test_emoji_prompt_preserved() -> None:
    prompt = "emoji 🎉✨"
    assert _handoff_argv(prompt)[-1] == prompt


def test_windows_path_prompt_preserved() -> None:
    prompt = r"C:\Users\<用户名>\Some Path\file.py"
    assert _handoff_argv(prompt)[-1] == prompt


def test_no_shell_true_with_user_text() -> None:
    source = (PROJECT_DIR / "ui" / "process_launcher.py").read_text(encoding="utf-8")
    for forbidden in ("shell=True", "cmd /c", "Invoke-Expression", "eval(", "Popen"):
        assert forbidden not in source, f"process_launcher must not use {forbidden}"


def test_user_text_never_built_into_command_string() -> None:
    for sample in MALICIOUS_SAMPLES:
        with patch.object(ProcessLauncher, "_resolve_native", return_value=FAKE_NATIVE):
            program, argv = ProcessLauncher._handoff_command(
                "codex", r"C:\fake\codex.CMD", sample
            )
        assert program == r"C:\node\node.exe", "program must be a native executable"
        assert isinstance(argv, list)
        assert argv == [r"C:\codex.js", sample], (
            f"prompt must be one final argv item, never a joined shell string: {sample!r}"
        )


def test_handoff_never_uses_powershell_wrapper() -> None:
    with tempfile.TemporaryDirectory() as td:
        with patch("ui.process_launcher.find_executable", return_value=r"C:\fake\codex.CMD"), patch(
            "ui.process_launcher._wrapped_cli_args", side_effect=AssertionError("wrapper must not be used")
        ) as wrap, patch.object(
            ProcessLauncher, "_resolve_native", return_value=FAKE_NATIVE
        ), patch.object(ProcessLauncher, "_spawn_native", return_value=(True, 1)):
            ok, _msg = ProcessLauncher.launch_agent(
                "codex", Path(td), initial_prompt="重构这个模块"
            )
            assert ok
            wrap.assert_not_called()


def test_launch_agent_delivers_single_argv_item() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        for sample in MALICIOUS_SAMPLES:
            with patch.object(ProcessLauncher, "_spawn_native", return_value=(True, 1)) as spawn, patch(
                "ui.process_launcher.find_executable", return_value=r"C:\fake\codex.CMD"
            ), patch.object(ProcessLauncher, "_resolve_native", return_value=FAKE_NATIVE):
                ok, _msg = ProcessLauncher.launch_agent("codex", ws, initial_prompt=sample)
                assert ok, f"launch must succeed for {sample!r}"
                program, argv = spawn.call_args[0][0], spawn.call_args[0][1]
                assert argv[-1] == sample
                assert argv.count(sample) == 1
                assert program == r"C:\node\node.exe"


def test_resolve_native_real_install() -> None:
    """On a real npm install, handoff resolves to a native exe, never a shim."""
    for agent in ("codex", "claude"):
        exe = find_executable(agent)
        if not exe:
            continue
        program, extra = ProcessLauncher._resolve_native(agent, exe)
        assert Path(program).suffix.lower() not in {".cmd", ".bat", ".ps1", ".psm1"}, (
            f"{agent} handoff must resolve to a native executable, got {program}"
        )
        assert isinstance(extra, list)


def test_launch_refuses_flag_prefix_prompt() -> None:
    with tempfile.TemporaryDirectory() as td:
        with patch("ui.process_launcher.find_executable", return_value=r"C:\fake\codex.CMD"):
            ok, msg = ProcessLauncher.launch_agent(
                "codex", Path(td), initial_prompt="--dangerously-skip-permissions"
            )
            assert not ok
            assert "flag" in msg or "Open only" in msg


def test_launch_refuses_subcommand_prompt() -> None:
    with tempfile.TemporaryDirectory() as td:
        with patch("ui.process_launcher.find_executable", return_value=r"C:\fake\codex.CMD"):
            ok, msg = ProcessLauncher.launch_agent("codex", Path(td), initial_prompt="exec")
            assert not ok
            assert "subcommand" in msg


def test_launch_blocks_when_no_native_target() -> None:
    """If the shim cannot be resolved to a native exe, refuse instead of corrupting."""
    with tempfile.TemporaryDirectory() as td:
        with patch("ui.process_launcher.find_executable", return_value=r"C:\fake\codex.CMD"), patch(
            "ui.process_launcher.shutil.which", return_value=None
        ):
            ok, msg = ProcessLauncher.launch_agent(
                "codex", Path(td), initial_prompt="重构这个模块"
            )
            assert not ok, "must refuse when the native target is unresolvable"
            assert "Open only" in msg


# -- 28-33. no fake send / no regression ------------------------------------

def test_chatgpt_has_no_send_handoff(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("用 ChatGPT 改这段代码")
        card = shell.recommendation_card
        assert card.pending_agent == "chatgpt"
        assert card._primary_btn.text() == "Open ChatGPT"
        assert "Send" not in card._primary_btn.text()


def test_vision_has_no_send_handoff(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("分析这张图片")
        card = shell.recommendation_card
        assert "Vision isn't available here yet" in card._title.text()
        assert not card._primary_btn.isVisible()


def test_claude_short_talk_unaffected(shell) -> None:
    with patch.object(shell.quick_ask, "ask", return_value=True) as ask_mock:
        _open_input(shell)
        shell._on_short_ask_send("为什么这里报错")
        assert ask_mock.call_count == 1
        assert ask_mock.call_args[0][0] == "claude"
        assert not shell.recommendation_card.has_pending


def test_codex_route_no_regression(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        card = shell.recommendation_card
        assert card.has_pending
        assert card.pending_agent == "codex"
        assert card._status.text() == "Recommended · Codex"


def test_router_still_zero_llm() -> None:
    from test_phase9a_agent_router import _forbidden_in_module

    paths = (
        PROJECT_DIR / "core" / "routing_models.py",
        PROJECT_DIR / "core" / "agent_router.py",
        PROJECT_DIR / "core" / "handoff.py",
    )
    for path in paths:
        assert not _forbidden_in_module(
            path, ("PySide6", "qtpy", "PyQt", "QProcess", "QWidget", "QObject", "QTimer", "Signal")
        ), f"{path.name} must stay Qt-free"
        assert not _forbidden_in_module(
            path, ("subprocess", "Popen", "startDetached", "taskkill", "socket", "urllib", "requests")
        ), f"{path.name} must stay subprocess/network-free"


def test_handoff_ui_no_internal_tokens(shell) -> None:
    with patch.object(shell.quick_ask, "ask"):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        card = shell.recommendation_card
        shown = " ".join(
            [
                card._title.text(),
                card._status.text(),
                card._primary_btn.text(),
                card._secondary_btn.text(),
                card._light_btn.text(),
            ]
        )
        for token in ("90", "82", "100", "Confidence", "best_for_coding", "requires_confirmation"):
            assert token not in shown, f"internal token leaked into UI: {token}"


# -- 34-36. telemetry / compat / protected files ----------------------------

def test_prompt_not_in_telemetry(shell, tmp_metrics: Path) -> None:
    prompt = "重构这个模块"
    shell.handoff_metrics = HandoffMetrics(tmp_metrics)
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send(prompt)
        shell.recommendation_card._primary_btn.click()
    latest = tmp_metrics / "handoff_latest.json"
    assert latest.exists()
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert prompt not in json.dumps(payload, ensure_ascii=False), "prompt must not be telemetry"
    assert "handoff_hash" in payload and "duration_ms" in payload
    tele = HandoffTelemetry(
        handoff_hash=payload["handoff_hash"],
        agent=payload["agent"],
        workspace=payload["workspace"],
        created_at=payload["created_at"],
        duration_ms=payload["duration_ms"],
        state=payload["state"],
    )
    assert tele.validate_safe() == []


def test_telemetry_workspace_is_token(shell) -> None:
    ws = shell.workspace_manager.current()
    token = workspace_token(str(ws))
    assert "/" not in token and "\\" not in token, "workspace telemetry must be a token"


def test_open_only_full_old_launcher_compat() -> None:
    with tempfile.TemporaryDirectory() as td:
        with patch.object(ProcessLauncher, "_spawn_detached", return_value=(True, 1)) as spawn, patch(
            "ui.process_launcher.find_executable", return_value=r"C:\fake\codex.ps1"
        ):
            ok, _msg = ProcessLauncher.launch_agent("codex", Path(td))
            assert ok
            assert spawn.call_args.kwargs.get("use_wt") is True, "Open only keeps the wt tab"
            assert "initial_prompt" not in spawn.call_args.kwargs


def test_protected_lifecycle_files_untouched(shell) -> None:
    before = _snapshot_sources()
    with patch.object(shell.quick_ask, "ask"), patch.object(
        ProcessLauncher, "launch_agent", return_value=(True, "ok")
    ):
        _open_input(shell)
        shell._on_short_ask_send("重构这个模块")
        shell.recommendation_card._primary_btn.click()
        shell.recommendation_card._light_btn.click()  # 9D.3: light = Open only
    assert _snapshot_sources() == before


def test_open_only_compat_old_signature() -> None:
    """The old 9B call site launch_agent(agent, workspace) still works."""
    with tempfile.TemporaryDirectory() as td:
        with patch.object(ProcessLauncher, "_spawn_detached", return_value=(True, 1)):
            ok, msg = ProcessLauncher.launch_agent("codex", Path(td))
            assert ok and msg


# -- handoff model unit tests ----------------------------------------------

def test_handoff_state_enum() -> None:
    assert [s.value for s in HandoffState] == [
        "pending", "launching", "handed_off", "failed", "cancelled",
    ]
    assert "success" not in {s.value for s in HandoffState}, (
        "handoff lifecycle must never contain task success"
    )


def test_make_handoff_request_fields() -> None:
    req = make_handoff_request("codex", "E:/Work", "重构", requires_confirmation=True)
    assert req.agent_id == "codex"
    assert req.workspace == "E:/Work"
    assert req.text == "重构"
    assert req.requires_confirmation
    assert req.handoff_id and req.created_at


def test_handoff_transport_verified() -> None:
    assert HANDOFF_TRANSPORT_VERIFIED == frozenset({"claude", "codex"})
    assert "chatgpt" not in HANDOFF_TRANSPORT_VERIFIED


_TMP_METRICS: Path | None = None


def _run(fn) -> None:
    """Run a test, feeding the shared shell / temp-metrics fixture as needed."""
    import inspect

    sig = inspect.signature(fn)
    params = list(sig.parameters)
    if params == ["shell"]:
        _prepare(_SHELL)
        fn(_SHELL)
    elif params == ["shell", "tmp_metrics"]:
        _prepare(_SHELL)
        fn(_SHELL, _TMP_METRICS)
    else:
        fn()


_SHELL = None


def main() -> None:
    global _SHELL, _TMP_METRICS
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    _SHELL = VisualShell(None, workspace_settings_file=Path(tempfile.mkdtemp(prefix="fap9c_ws_")) / "ui_settings.json")
    _TMP_METRICS = Path(tempfile.mkdtemp(prefix="fap9c_metrics_"))
    tests = [
        test_recommendation_does_not_auto_execute,
        test_send_click_creates_handoff,
        test_open_only_creates_no_handoff,
        test_light_open_only_creates_no_handoff,
        test_prompt_passed_verbatim_to_codex,
        test_agent_locked,
        test_workspace_locked,
        test_workspace_change_invalidates_pending,
        test_workspace_drift_cancels_send,
        test_double_click_launches_once,
        test_launching_disables_primary,
        test_spawn_success_handed_off,
        test_handed_off_not_task_success,
        test_spawn_failure_failed,
        test_launch_failure_no_external_lifecycle,
        test_no_managed_session_created,
        test_external_lifecycle_semantics_untouched,
        test_permission_card_not_auto_approved,
        test_quote_prompt_safe,
        test_newline_prompt_safe,
        test_semicolon_prompt_safe,
        test_ampersand_prompt_safe,
        test_pipe_prompt_safe,
        test_powershell_expression_stays_text,
        test_backtick_prompt_safe,
        test_unicode_prompt_preserved,
        test_emoji_prompt_preserved,
        test_windows_path_prompt_preserved,
        test_no_shell_true_with_user_text,
        test_user_text_never_built_into_command_string,
        test_handoff_never_uses_powershell_wrapper,
        test_launch_agent_delivers_single_argv_item,
        test_resolve_native_real_install,
        test_launch_refuses_flag_prefix_prompt,
        test_launch_refuses_subcommand_prompt,
        test_launch_blocks_when_no_native_target,
        test_chatgpt_has_no_send_handoff,
        test_vision_has_no_send_handoff,
        test_claude_short_talk_unaffected,
        test_codex_route_no_regression,
        test_router_still_zero_llm,
        test_handoff_ui_no_internal_tokens,
        test_prompt_not_in_telemetry,
        test_telemetry_workspace_is_token,
        test_open_only_full_old_launcher_compat,
        test_protected_lifecycle_files_untouched,
        test_open_only_compat_old_signature,
        test_handoff_state_enum,
        test_make_handoff_request_fields,
        test_handoff_transport_verified,
    ]
    try:
        for fn in tests:
            _run(fn)
        print(f"Phase 9C native handoff tests passed ({len(tests)} tests).")
    finally:
        _SHELL.shutdown()


if __name__ == "__main__":
    main()
