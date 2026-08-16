"""Qt-free tests for the Phase 7A persistent workspace store."""

import json
import tempfile
from pathlib import Path

from ui.workspace_store import MAX_RECENT_WORKSPACES, WorkspaceStore


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        settings = root / "config" / "ui_settings.json"
        workspaces = []
        for i in range(7):
            p = root / f"ws{i}"
            p.mkdir()
            workspaces.append(p)

        store = WorkspaceStore(settings, workspaces[0])
        assert store.current_workspace == workspaces[0]
        for p in workspaces:
            store.set_workspace(p)
        assert store.current_workspace == workspaces[-1].resolve()
        assert len(store.recent_workspaces) == MAX_RECENT_WORKSPACES
        assert store.recent_workspaces[0] == workspaces[-1].resolve()

        parsed = json.loads(settings.read_text(encoding="utf-8"))
        assert parsed["current_workspace"] == str(workspaces[-1].resolve())
        assert len(parsed["recent_workspaces"]) == MAX_RECENT_WORKSPACES

        reloaded = WorkspaceStore(settings, workspaces[0])
        assert reloaded.current_workspace == workspaces[-1].resolve()

    print("Phase 7A core tests passed.")


if __name__ == "__main__":
    main()
