"""Create the ``Firefly AI Pet`` desktop shortcut (explicit, run-once installer).

Usage:
    .venv\\Scripts\\python.exe tools\\install_desktop_shortcut.py

Creates (or overwrites) a ``Firefly AI Pet.lnk`` on the current user's desktop
that launches ``.venv\\Scripts\\pythonw.exe app.py`` with the project directory
as its working directory and the Firefly icon. Exits 0 on success, non-zero and
a message on failure. This is intentionally a manual step; a future installer
will take it over.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core import windows_shortcuts


def main() -> int:
    try:
        spec = windows_shortcuts.launch_spec()
        if not Path(spec.target).is_file():
            print(f"ERROR: launch target not found: {spec.target}", file=sys.stderr)
            print("Recreate the virtual environment, then retry.", file=sys.stderr)
            return 2
        windows_shortcuts.create_desktop_shortcut()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    path = windows_shortcuts.desktop_shortcut_path()
    print(f"Created desktop shortcut: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
