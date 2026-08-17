"""Qt-free Windows ``.lnk`` helpers for Firefly launch shortcuts.

The single source of truth for *how* Firefly is launched from a shell shortcut
lives in :func:`launch_spec`. Both the desktop shortcut and the Startup-folder
autostart shortcut are built from that one spec, so the two can never drift.

``.lnk`` files are created and read through Windows' built-in WScript.Shell COM
object via a short-lived PowerShell process (no pywin32, no new dependency).
All values cross the process boundary as one base64-encoded JSON object, so
paths containing spaces, quotes, or non-ASCII characters never corrupt the
command line. Existence checks and deletion are plain filesystem operations.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SHORTCUT_NAME = "Firefly AI Pet"
VENV_PYTHONW = PROJECT_DIR / ".venv" / "Scripts" / "pythonw.exe"
APP_SCRIPT = PROJECT_DIR / "app.py"
ICON_PATH = PROJECT_DIR / "assets" / "firefly.ico"


@dataclass(frozen=True)
class ShortcutSpec:
    """The invariant launch definition shared by every Firefly shortcut."""

    target: str
    arguments: str
    working_directory: str
    icon_path: str
    icon_index: int = 0
    description: str = SHORTCUT_NAME

    def icon_location(self) -> str:
        return f"{self.icon_path},{self.icon_index}"


def launch_spec() -> ShortcutSpec:
    """The production GUI launch target: ``pythonw.exe app.py`` (no console)."""
    return ShortcutSpec(
        target=str(VENV_PYTHONW),
        arguments=f'"{APP_SCRIPT}"',
        working_directory=str(PROJECT_DIR),
        icon_path=str(ICON_PATH),
    )


# -- PowerShell plumbing ---------------------------------------------------


def _is_windows() -> bool:
    return os.name == "nt"


def _powershell_exe() -> str:
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if not exe:
        raise RuntimeError("PowerShell is not available.")
    return exe


def _run_powershell(script: str) -> str:
    """Run a PowerShell snippet; return trimmed stdout, raise on failure.

    On Windows the child is launched windowless (CREATE_NO_WINDOW + a hidden
    STARTUPINFO) so a background GUI runtime never flashes a console. stdout,
    stderr and the exit code are still captured, so a non-zero exit still
    raises and its stderr is preserved.
    """
    result = subprocess.run(
        [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
        **_windows_no_console_kwargs(),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(stderr or f"PowerShell exited {result.returncode}.")
    return result.stdout


def _windows_no_console_kwargs() -> dict:
    """subprocess.run() kwargs that keep the child windowless on Windows."""
    if not _is_windows():
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _encode_json(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def special_folder(name: str) -> Path | None:
    """Resolve a Windows known folder via .NET (handles OneDrive/localization).

    Returns None when the folder is unavailable (or on non-Windows). ``name``
    must be a fixed literal such as ``"Desktop"`` or ``"Startup"``.
    """
    if not _is_windows():
        return None
    script = (
        f"$p = [Environment]::GetFolderPath('{name}')\n"
        "if ($p) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p)) }"
    )
    out = _run_powershell(script).strip()
    if not out:
        return None
    return Path(base64.b64decode(out).decode("utf-8"))


# -- core .lnk operations --------------------------------------------------


def create_shortcut(lnk_path: Path | str, spec: ShortcutSpec) -> None:
    """Create (or overwrite) a ``.lnk``; raises RuntimeError on any failure."""
    if not _is_windows():
        raise RuntimeError("Shortcut creation requires Windows.")
    lnk_path = Path(lnk_path)
    lnk_path.parent.mkdir(parents=True, exist_ok=True)

    payload = _encode_json(
        {
            "lnkPath": str(lnk_path),
            "target": spec.target,
            "arguments": spec.arguments,
            "workingDirectory": spec.working_directory,
            "iconLocation": spec.icon_location(),
            "description": spec.description,
        }
    )
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) "
        "| ConvertFrom-Json\n"
        "$ws = New-Object -ComObject WScript.Shell\n"
        "$lnk = $ws.CreateShortcut($p.lnkPath)\n"
        "$lnk.TargetPath = $p.target\n"
        "$lnk.Arguments = $p.arguments\n"
        "$lnk.WorkingDirectory = $p.workingDirectory\n"
        "$lnk.IconLocation = $p.iconLocation\n"
        "$lnk.Description = $p.description\n"
        "$lnk.Save()"
    )
    _run_powershell(script)


def shortcut_exists(lnk_path: Path | str | None) -> bool:
    """Existence is the source of truth for whether a shortcut is installed."""
    if lnk_path is None:
        return False
    return Path(lnk_path).is_file()


def remove_shortcut(lnk_path: Path | str | None) -> bool:
    """Delete the shortcut; returns True when something was actually removed."""
    if lnk_path is None:
        return False
    path = Path(lnk_path)
    try:
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed
    except OSError:
        return False


def read_shortcut(lnk_path: Path | str) -> dict | None:
    """Read back a shortcut's launch properties; None if it does not exist."""
    lnk_path = Path(lnk_path)
    if not _is_windows() or not lnk_path.is_file():
        return None
    payload = _encode_json({"lnkPath": str(lnk_path)})
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) "
        "| ConvertFrom-Json\n"
        "$ws = New-Object -ComObject WScript.Shell\n"
        "$lnk = $ws.CreateShortcut($p.lnkPath)\n"
        "[ordered]@{ target = $lnk.TargetPath; arguments = $lnk.Arguments; "
        "workingDirectory = $lnk.WorkingDirectory; iconLocation = $lnk.IconLocation } "
        "| ConvertTo-Json -Compress"
    )
    out = _run_powershell(script).strip()
    if not out:
        return None
    try:
        data = json.loads(out)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None
    except ValueError:
        return None


# -- desktop shortcut -------------------------------------------------------


def desktop_dir() -> Path | None:
    return special_folder("Desktop")


def desktop_shortcut_path() -> Path | None:
    folder = desktop_dir()
    return folder / f"{SHORTCUT_NAME}.lnk" if folder is not None else None


def create_desktop_shortcut() -> None:
    path = desktop_shortcut_path()
    if path is None:
        raise RuntimeError("Desktop folder unavailable.")
    create_shortcut(path, launch_spec())


def remove_desktop_shortcut() -> bool:
    return remove_shortcut(desktop_shortcut_path())


def desktop_shortcut_exists() -> bool:
    return shortcut_exists(desktop_shortcut_path())
