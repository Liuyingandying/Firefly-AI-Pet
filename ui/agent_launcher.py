"""Launcher for the dock's four agent buttons (Claude / Codex / Qwen / Z Code).

Every CLI launcher runs the executable as a direct argv list with the
selected workspace as cwd (never shell string concatenation), opening a
visible independent console. Any failure returns a friendly (ok, message)
pair and must never raise into the UI.

Qwen YOLO: Qwen Code has no stable CLI flag for YOLO; the official
configuration surface is ``tools.approvalMode = "yolo"`` in the per-project
``.qwen/config.json``. To enter YOLO from the CLI layer (no GUI automation)
we write a minimal per-launch config file in the selected workspace and pass
``QWEN_HOME``/``QWEN_CONFIG_DIR``-style overrides so the launched Qwen Code
reads it as its own config.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CLAUDE_FLAGS = ("--dangerously-skip-permissions",)
# Codex intentionally runs with the user's normal default configuration.

QWEN_YOLO_CONFIG = {"tools": {"approvalMode": "yolo"}}
QWEN_PROFILE_DIRNAME = "qwen-yolo"
QWEN_SETTINGS_FILENAME = "settings.json"

# Z Code desktop executable, discovered once (no hard-coded guess).
_ZCODE_EXE_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ZCode" / "ZCode.exe",
)

LAUNCHER_DISPLAY = {
    "claude": "Claude",
    "codex": "Codex",
    "qwen": "Qwen",
    "zcode": "Z Code",
}


def _find_executable(name: str) -> str | None:
    return shutil.which(name)


def _spawn_cli(argv: list[str], workspace: Path) -> tuple[bool, str]:
    """Start argv in a new visible console with cwd=workspace."""
    creation = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(
            argv,
            cwd=str(workspace),
            creationflags=creation,
            close_fds=True,
        )
    except OSError as exc:
        logger.warning("launcher spawn failed: %s: %s", type(exc).__name__, exc)
        return False, "launch failed"
    return True, "ok"


def _ensure_workspace(workspace: Path) -> tuple[bool, str]:
    if not workspace.is_dir():
        return False, "Workspace unavailable"
    return True, "ok"


def _wrapped_cmd_argv(cli_exe: str, cli_args: list[str]) -> list[str]:
    """Run a .cmd/.bat CLI shim through cmd.exe so the interactive terminal
    stays open.

    ``cmd.exe /d /k call <shim> <args>`` keeps the console alive (/k) even if
    the CLI exits, and ``call`` preserves the shim's own batch semantics.
    PowerShell ``-File`` is NOT used for npm .cmd shims (it made the console
    flash and close). Native .exe paths are passed through unchanged.
    """
    if not cli_exe.lower().endswith((".cmd", ".bat", ".ps1")):
        return [cli_exe, *cli_args]
    return ["cmd.exe", "/d", "/k", "call", cli_exe, *cli_args]


# ----------------------------------------------------------------- launchers


def launch_claude(workspace: Path) -> tuple[bool, str]:
    ok, msg = _ensure_workspace(workspace)
    if not ok:
        return False, msg
    exe = _find_executable("claude")
    if not exe:
        return False, "Claude CLI not found"
    return _spawn_cli(_wrapped_cmd_argv(exe, list(CLAUDE_FLAGS)), workspace)


def launch_codex(workspace: Path) -> tuple[bool, str]:
    ok, msg = _ensure_workspace(workspace)
    if not ok:
        return False, msg
    exe = _find_executable("codex")
    if not exe:
        return False, "Codex CLI not found"
    # Default config only: no --yolo / dangerous / full-auto flags.
    return _spawn_cli(_wrapped_cmd_argv(exe, []), workspace)


def _qwen_profile_dir() -> Path:
    """Firefly-managed YOLO profile, always OUTSIDE the selected workspace.

    %LOCALAPPDATA%\FireflyAI\qwen-yolo\ — never <workspace>\.qwen\."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "FireflyAI"
    return base / QWEN_PROFILE_DIRNAME


def _qwen_profile_settings_file() -> Path:
    return _qwen_profile_dir() / QWEN_SETTINGS_FILENAME


def _user_qwen_home() -> Path:
    """The user's real Qwen home, kept read-only (never modified)."""
    return Path(os.environ.get("QWEN_HOME") or (Path(os.environ.get("APPDATA", "~")) / "qwen"))


def launch_qwen_yolo(workspace: Path) -> tuple[bool, str]:
    """Launch Qwen Code in YOLO mode.

    The selected workspace is used ONLY as the subprocess cwd. The YOLO
    approval mode comes from a Firefly-managed profile OUTSIDE the workspace
    (%LOCALAPPDATA%\FireflyAI\qwen-yolo\settings.json), so the user's
    project tree is never written to.
    """
    ok, msg = _ensure_workspace(workspace)
    if not ok:
        return False, msg
    exe = _find_executable("qwen")
    if not exe:
        return False, "Qwen CLI not found"

    profile_file = _qwen_profile_settings_file()
    user_home = _user_qwen_home()
    try:
        profile_file.parent.mkdir(parents=True, exist_ok=True)
        # Inherit the user's real settings where present, then override ONLY
        # the YOLO field. Never touches the user's own files.
        existing = {}
        user_settings = user_home / QWEN_SETTINGS_FILENAME
        if user_settings.is_file():
            try:
                existing = json.loads(user_settings.read_text(encoding="utf-8"))
            except ValueError:
                existing = {}
        existing.setdefault("tools", {}).update(QWEN_YOLO_CONFIG["tools"])
        profile_file.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("qwen yolo profile write failed: %s", exc)
        return False, "launch failed"

    env = dict(os.environ)
    # Point Qwen Code's user-settings root at the Firefly profile: the
    # launched CLI resolves `~/.qwen/settings.json` under this QWEN_HOME and
    # reads approvalMode=yolo, while its own config (model/provider/etc.)
    # from the real QWEN_HOME is preserved via inheritance above.
    env["QWEN_HOME"] = str(profile_file.parent)
    env.pop("QWEN_CONFIG_DIR", None)
    env.pop("QWEN_CONFIG_PATH", None)

    creation = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(
            _wrapped_cmd_argv(exe, []),
            cwd=str(workspace),
            env=env,
            creationflags=creation,
            close_fds=True,
        )
    except OSError as exc:
        logger.warning("qwen spawn failed: %s: %s", type(exc).__name__, exc)
        return False, "launch failed"
    return True, "ok"


def _discover_zcode() -> Path | None:
    for candidate in _ZCODE_EXE_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("ZCode.exe") or shutil.which("zcode")
    return Path(found) if found else None


def launch_zcode(workspace: Path) -> tuple[bool, str]:
    """Launch/activate Z Code (no CLI, no workspace cwd semantics)."""
    exe = _discover_zcode()
    if exe is None:
        return False, "Z Code executable not found"
    try:
        subprocess.Popen([str(exe)], close_fds=True)
    except OSError as exc:
        logger.warning("zcode launch failed: %s: %s", type(exc).__name__, exc)
        return False, "launch failed"
    return True, "ok"
