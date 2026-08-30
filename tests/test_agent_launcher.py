"""Launcher / UX v1 tests: approval-card close + four agent launchers.

Coverage: approval close semantics (A-E), workspace cwd routing (F/K/L),
CLI argv flags (G/J), graceful failures (M/N), Z Code semantics (O/P),
and no side effects on unrelated systems (Q). Real CLI executables are
discovered but never launched; Popen is faked.
"""

import json
import subprocess
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from ui import agent_launcher
from ui.agent_launcher import (
    CLAUDE_FLAGS,
    launch_claude,
    launch_codex,
    launch_qwen_yolo,
    launch_zcode,
)
from ui.permission_card import PermissionCard


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def fake_workspace(tmp_path):
    return tmp_path / "my space (cjk 目录)"  # spaces + CJK + parens


# ================================================== A-E approval card


def test_a_card_has_close_button(qapp):
    card = PermissionCard()
    assert card._close_btn is not None
    assert card._close_btn.text() == "×"


def test_b_close_hides_card(qapp):
    card = PermissionCard()
    card.show_for(["codex"], "codex")
    assert card.isVisible()
    card._close_btn.click()
    assert not card.isVisible()


def test_c_close_does_not_touch_task_state(qapp, monkeypatch):
    card = PermissionCard()
    card.show_for(["codex"], "codex")
    # View stays enabled after close (episode not consumed), and no
    # task-cancel path is invoked: close only hides.
    card._close_btn.click()
    assert not card.isVisible()
    assert card._primary_agent is None  # UI-only dismissal


def test_d_new_approval_can_reappear(qapp):
    card = PermissionCard()
    card.show_for(["codex"], "codex")
    card._close_btn.click()
    # A different agent episode arrives -> shows again
    card.show_for(["claude"], "claude")
    assert card.isVisible()


def test_e_same_episode_stays_dismissed(qapp):
    card = PermissionCard()
    card.show_for(["codex"], "codex")
    card._close_btn.click()
    # Repeated refresh of the same episode stays hidden
    card.show_for(["codex"], "codex")
    assert not card.isVisible()
    # ...until the waiting state ends (episode boundary), then it may show
    card.clear_dismissed()
    card.show_for(["codex"], "codex")
    assert card.isVisible()


# ================================================== launcher basics


def test_claude_flags_are_real(monkeypatch, fake_workspace):
    fake_workspace.mkdir()
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "claude.cmd")
    argv = []
    monkeypatch.setattr(
        agent_launcher.subprocess, "Popen",
        lambda a, **k: argv.append((a, k)) or True,
    )
    ok, msg = launch_claude(fake_workspace)
    assert ok
    argv_list = argv[0][0]
    assert argv_list[0].lower().endswith("cmd.exe")  # .cmd shim -> cmd.exe
    assert argv_list[1:4] == ["/d", "/k", "call"]
    # The shim itself is passed with its full path
    assert argv_list[4] == "claude.cmd"
    assert "--dangerously-skip-permissions" in argv_list
    assert argv[0][1]["cwd"] == str(fake_workspace)


def test_codex_default_config_no_yolo(monkeypatch, fake_workspace):
    fake_workspace.mkdir()
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "codex.cmd")
    argv = []
    monkeypatch.setattr(agent_launcher.subprocess, "Popen",
                        lambda a, **k: argv.append((a, k)) or True)
    ok, msg = launch_codex(fake_workspace)
    assert ok
    argv_list = argv[0][0]
    assert argv_list[0].lower().endswith("cmd.exe")
    assert argv_list[1:4] == ["/d", "/k", "call"]
    assert argv_list[4] == "codex.cmd"
    assert all(flag not in " ".join(argv_list) for flag in ("--yolo", "dangerously", "full-auto"))
    assert argv[0][1]["cwd"] == str(fake_workspace)


def test_qwen_uses_workspace_cwd_and_external_profile(monkeypatch, fake_workspace, tmp_path):
    fake_workspace.mkdir()
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "qwen.cmd")
    profile_dir = tmp_path / "FireflyAI" / "qwen-yolo"
    monkeypatch.setattr(agent_launcher, "_qwen_profile_dir", lambda: profile_dir)
    monkeypatch.setattr(
        agent_launcher, "_user_qwen_home",
        lambda: fake_workspace / "no-user-settings-here",
    )
    argv = []
    monkeypatch.setattr(agent_launcher.subprocess, "Popen",
                        lambda a, **k: argv.append((a, k)) or True)
    ok, msg = launch_qwen_yolo(fake_workspace)
    assert ok
    # A: cwd is the selected workspace (only)
    assert argv[0][1]["cwd"] == str(fake_workspace)
    assert argv[0][0][0].lower().endswith("cmd.exe")
    assert argv[0][0][1:4] == ["/d", "/k", "call"]
    # B: the YOLO profile lives OUTSIDE the workspace
    assert profile_dir.is_relative_to(tmp_path) and not profile_dir.is_relative_to(fake_workspace)
    # F: YOLO mode is configured in the external profile
    settings = json.loads((profile_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["tools"]["approvalMode"] == "yolo"
    # C: nothing was created inside the workspace
    assert not (fake_workspace / ".qwen").exists()
    # the launched CLI is pointed at the profile via QWEN_HOME
    assert argv[0][1]["env"]["QWEN_HOME"] == str(profile_dir)


def test_qwen_never_writes_into_workspace(monkeypatch, fake_workspace, tmp_path):
    """D: an existing workspace .qwen is left untouched by the launcher."""
    fake_workspace.mkdir()
    existing_qwen = fake_workspace / ".qwen"
    existing_qwen.mkdir()
    existing_settings = existing_qwen / "settings.json"
    original_content = '{"general": {"language": "zh-CN"}}'
    existing_settings.write_text(original_content, encoding="utf-8")

    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "qwen.cmd")
    profile_dir = tmp_path / "FireflyAI" / "qwen-yolo"
    monkeypatch.setattr(agent_launcher, "_qwen_profile_dir", lambda: profile_dir)
    monkeypatch.setattr(agent_launcher, "_user_qwen_home",
                        lambda: fake_workspace / "no-user-settings-here")
    monkeypatch.setattr(agent_launcher.subprocess, "Popen", lambda a, **k: True)

    ok, _ = launch_qwen_yolo(fake_workspace)
    assert ok
    # the workspace's own .qwen/settings.json is byte-identical
    assert existing_settings.read_text(encoding="utf-8") == original_content
    # no new files appeared in the workspace tree
    created = sorted(str(p.relative_to(fake_workspace)) for p in fake_workspace.rglob("*"))
    assert created == [".qwen", str(Path(".qwen") / "settings.json")]


def test_qwen_profile_inherits_user_settings(monkeypatch, fake_workspace, tmp_path):
    """The Firefly profile inherits the user's real settings and only
    overrides the YOLO field."""
    fake_workspace.mkdir()
    user_home = tmp_path / "user-qwen-home"
    user_home.mkdir()
    (user_home / "settings.json").write_text(
        '{"model": "qwen3.6", "tools": {"approvalMode": "auto"}}', encoding="utf-8"
    )
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "qwen.cmd")
    profile_dir = tmp_path / "FireflyAI" / "qwen-yolo"
    monkeypatch.setattr(agent_launcher, "_qwen_profile_dir", lambda: profile_dir)
    monkeypatch.setattr(agent_launcher, "_user_qwen_home", lambda: user_home)
    monkeypatch.setattr(agent_launcher.subprocess, "Popen", lambda a, **k: True)

    ok, _ = launch_qwen_yolo(fake_workspace)
    assert ok
    settings = json.loads((profile_dir / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "qwen3.6"          # user setting preserved
    assert settings["tools"]["approvalMode"] == "yolo"  # YOLO overridden
    # user's own file untouched
    assert json.loads((user_home / "settings.json").read_text(encoding="utf-8"))[
        "tools"]["approvalMode"] == "auto"


def test_workspace_switch_is_followed(monkeypatch, tmp_path):
    """K: launcher reads the manager's current value at click time, so a
    workspace switch takes effect without restart."""
    from core.workspace_manager import WorkspaceManager

    manager = WorkspaceManager()
    first = tmp_path / "first"; first.mkdir()
    second = tmp_path / "second"; second.mkdir()
    manager.set_current(first)
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "claude.cmd")
    argv = []
    monkeypatch.setattr(agent_launcher.subprocess, "Popen",
                        lambda a, **k: argv.append((a, k)) or True)
    launch_claude(manager.current())
    assert argv[-1][1]["cwd"] == str(first)
    manager.set_current(second)  # user switches workspace
    launch_claude(manager.current())
    assert argv[-1][1]["cwd"] == str(second)


def test_path_with_spaces_and_cjk_used_as_cwd(monkeypatch, fake_workspace):
    """L: spaces/CJK/parens must survive — direct argv + cwd, no shell."""
    fake_workspace.mkdir()
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "claude.cmd")
    argv = []
    monkeypatch.setattr(agent_launcher.subprocess, "Popen",
                        lambda a, **k: argv.append((a, k)) or True)
    ok, _ = launch_claude(fake_workspace)
    assert ok
    assert argv[0][1]["cwd"] == str(fake_workspace)


def test_no_powershell_file_for_shims():
    """The shim wrapper must never use powershell -File (console flash/close)."""
    import ast

    source = Path(agent_launcher.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "_wrapped_cmd_argv")
    # Only the executable body (skip the docstring) may not reference
    # powershell / -File. Crop to the first real statement's start.
    body_start = fn.body[1].lineno if len(fn.body) > 1 else fn.body[0].lineno
    source_lines = source.splitlines()
    code = "\n".join(source_lines[body_start - 1:])
    assert "powershell" not in code
    assert "-File" not in code
    assert "cmd.exe" in code and '"/k"' in code and '"call"' in code


def test_no_shell_string_building(monkeypatch, fake_workspace):
    """Workspace path is never spliced into a shell string."""
    fake_workspace.mkdir()
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "claude.cmd")
    seen = []
    monkeypatch.setattr(agent_launcher.subprocess, "Popen",
                        lambda a, **k: seen.append(a) or True)
    launch_claude(fake_workspace)
    joined = " ".join(seen[0])
    assert "&&" not in joined and "cd " not in joined
    # Source-level guard: the launcher module never shells out via os.system
    # or os.popen.
    source = Path(agent_launcher.__file__).read_text(encoding="utf-8")
    assert "os.system" not in source
    assert "os.popen" not in source


# ================================================== graceful failures


def test_workspace_missing_graceful(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "claude.cmd")
    ok, msg = launch_claude(tmp_path / "nope")
    assert not ok and "Workspace unavailable" in msg


def test_cli_missing_graceful(monkeypatch, fake_workspace):
    fake_workspace.mkdir()
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: None)
    for launch in (launch_claude, launch_codex, launch_qwen_yolo):
        ok, msg = launch(fake_workspace)
        assert not ok
        assert "not found" in msg


# ================================================== Z Code


def test_zcode_never_uses_workspace_cwd(monkeypatch, tmp_path):
    """O: Z Code is app-launch only; workspace is not a CLI cwd."""
    ws = tmp_path / "ws"; ws.mkdir()
    exe = tmp_path / "ZCode.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(agent_launcher, "_discover_zcode", lambda: exe)
    argv = []
    monkeypatch.setattr(agent_launcher.subprocess, "Popen",
                        lambda a, **k: argv.append((a, k)) or True)
    ok, _ = launch_zcode(ws)
    assert ok
    assert "cwd" not in argv[0][1] or argv[0][1].get("cwd") is None
    assert str(exe) in argv[0][0]


def test_zcode_missing_graceful():
    import os
    monkey = pytest.MonkeyPatch()
    monkey.setattr(agent_launcher, "_discover_zcode", lambda: None)
    try:
        ok, msg = launch_zcode(Path("."))
        assert not ok and "Z Code executable not found" in msg
    finally:
        monkey.undo()


# ================================================== Q no side effects


def test_q_launcher_has_no_screen_vision_or_provider_side_effects(
    monkeypatch, fake_workspace
):
    """Q: launching a CLI must never touch ScreenVision / providers / memory."""
    fake_workspace.mkdir()
    monkeypatch.setattr(agent_launcher, "_find_executable", lambda n: "codex.cmd")
    monkeypatch.setattr(agent_launcher.subprocess, "Popen",
                        lambda a, **k: True)
    for launch in (launch_claude, launch_codex, launch_qwen_yolo):
        ok, _ = launch(fake_workspace)
        assert ok
    # Pure subprocess launcher: no screen vision import, no provider call.
    assert "screen_vision" not in open(
        __import__("ui.agent_launcher", fromlist=["__file__"]).__file__,
        encoding="utf-8",
    ).read()
