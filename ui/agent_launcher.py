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

CLI_INSTALL_HINTS = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "codex": "npm install -g @openai/codex",
    "qwen": "npm install -g @qwen-code/qwen-code@latest",
}

# Codex Microsoft Store desktop app: package-name prefix of its Appx AUMID.
_CODEX_PACKAGE_PREFIX = "OpenAI.Codex"


def _extra_candidate_dirs() -> list[Path]:
    """Well-known per-user CLI install dirs beyond the process PATH.

    A GUI-launched process can run with a PATH that predates a CLI install
    (npm prefix not refreshed, nvm shim, MSIX app-execution alias), so PATH
    alone misses CLIs that are actually installed. Every dir here is a
    standard Windows location — nothing machine-specific.
    """
    dirs: list[Path] = []
    prefix = (
        os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix")
    )
    if prefix:
        dirs.append(Path(prefix))
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "npm")
    nvm_symlink = os.environ.get("NVM_SYMLINK")
    if nvm_symlink:
        dirs.append(Path(nvm_symlink))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "WindowsApps")
    dirs.append(Path.home() / ".local" / "bin")
    return dirs


def _find_executable(name: str) -> str | None:
    """PATH first, then the standard per-user install dirs."""
    found = shutil.which(name)
    if found:
        return found
    seen: set[Path] = set()
    for d in _extra_candidate_dirs():
        if d in seen or not d.is_dir():
            continue
        seen.add(d)
        found = shutil.which(name, path=str(d))
        if found:
            return found
    return None


_APPX_AUMID_UNSET = object()
_codex_app_aumid_cache: object = _APPX_AUMID_UNSET


def _codex_app_aumid() -> str | None:
    """AUMID of the Codex Microsoft Store desktop app, cached; None if absent.

    ``OpenAI.Codex_<publisher>!<app-id>`` is the ``shell:AppsFolder`` launch
    target for the desktop GUI.  Queried once per process through the Appx
    catalog — nothing machine-specific is hardcoded.
    """
    global _codex_app_aumid_cache
    if _codex_app_aumid_cache is not _APPX_AUMID_UNSET:
        return _codex_app_aumid_cache  # type: ignore[return-value]
    aumid: str | None = None
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-StartApps | Where-Object AppID -like "
                f"'{_CODEX_PACKAGE_PREFIX}*') | "
                "Select-Object -First 1 -ExpandProperty AppID",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode == 0:
            aumid = out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        aumid = None
    _codex_app_aumid_cache = aumid
    return aumid


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
        return False, f"Claude CLI not found (install: {CLI_INSTALL_HINTS['claude']})"
    return _spawn_cli(_wrapped_cmd_argv(exe, list(CLAUDE_FLAGS)), workspace)


def launch_codex(workspace: Path) -> tuple[bool, str]:
    """Open Codex: the desktop app when installed, else the CLI console.

    The Codex Microsoft Store desktop app manages its own projects, so the
    workspace does not apply to it (same semantics as Z Code).  When the
    app is absent, the CLI fallback still runs inside the selected
    workspace console.
    """
    aumid = _codex_app_aumid()
    if aumid:
        try:
            os.startfile(f"shell:AppsFolder\\{aumid}")
            return True, "ok"
        except OSError as exc:
            logger.warning("codex desktop app launch failed: %s", exc)
    ok, msg = _ensure_workspace(workspace)
    if not ok:
        return False, msg
    exe = _find_executable("codex")
    if not exe:
        store_cli = _codex_store_cli()
        exe = str(store_cli) if store_cli else None
    if not exe:
        return False, (
            "Codex not found (install the Codex Microsoft Store app, or "
            f"{CLI_INSTALL_HINTS['codex']})"
        )
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
        return False, f"Qwen CLI not found (install: {CLI_INSTALL_HINTS['qwen']})"

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


def _uninstall_registry_zcode_exes() -> list[Path]:
    """Candidate ZCode.exe paths from Add/Remove-Programs metadata.

    Desktop apps register an Uninstall entry even when never put on PATH.
    ``InstallLocation`` is the install dir when present; ``DisplayIcon``
    usually points into it too (``<dir>\\app.ico`` or ``<dir>\\app.exe,0``).
    Scanning the registry keeps discovery machine-agnostic — no guesses.
    """
    import winreg

    exe_candidates: list[Path] = []
    uninstall_key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            root = winreg.OpenKey(hive, uninstall_key)
        except OSError:
            continue
        with root:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                display = None
                raw_values: list[str] = []
                try:
                    with winreg.OpenKey(root, subkey_name) as subkey:
                        display = str(winreg.QueryValueEx(subkey, "DisplayName")[0])
                        # All value reads must happen INSIDE this block —
                        # the handle is closed when the with exits.
                        for value_name in ("InstallLocation", "DisplayIcon"):
                            try:
                                raw_values.append(
                                    str(winreg.QueryValueEx(subkey, value_name)[0])
                                )
                            except OSError:
                                continue
                except OSError:
                    continue
                if not display or "zcode" not in display.lower():
                    continue
                for raw in raw_values:
                    raw = raw.split(",")[0].strip()
                    if not raw:
                        continue
                    p = Path(raw)
                    if p.suffix.lower() == ".exe":
                        exe_candidates.append(p)
                    else:
                        # An icon path (or a bare dir): the install dir is
                        # the file's parent / the path itself.
                        base = p.parent if p.suffix else p
                        exe_candidates.append(base / "ZCode.exe")
    return exe_candidates


def _codex_store_cli() -> Path | None:
    """CLI copy materialized by the Codex Microsoft Store app.

    The Store build registers no ``codex`` execution alias and the binary
    inside ``C:\\Program Files\\WindowsApps`` is ACL-protected, but the app
    unpacks a user-accessible copy to
    ``%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<build>\\codex.exe`` on first run
    (the same file its own tooling invokes).  The <build> segment is a
    version hash, so glob and keep the newest.
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    bin_root = Path(local) / "OpenAI" / "Codex" / "bin"
    best: Path | None = None
    best_mtime = -1.0
    try:
        for candidate in bin_root.glob("*/codex.exe"):
            if not candidate.is_file():
                continue
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = candidate, mtime
    except OSError:
        return None
    return best


def _discover_zcode() -> Path | None:
    for candidate in _ZCODE_EXE_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("ZCode.exe") or shutil.which("zcode")
    if found:
        return Path(found)
    for exe in _uninstall_registry_zcode_exes():
        try:
            if exe.is_file():
                return exe
        except OSError:
            continue
    return None


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
