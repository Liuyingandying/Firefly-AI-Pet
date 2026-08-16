"""StateMonitor lifecycle, fault tolerance, dedupe, and UI wiring tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtWidgets import QApplication

from core.models import LifecycleState
from core.state_monitor import StateMonitor
from ui.agent_dock import AgentDock
from ui.pet_overlay import PetOverlay


NOW = 1_000_000_000
STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}


def write_source(root: Path, agent_id: str, state: str, timestamp: int = NOW) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{agent_id}.json").write_text(
        json.dumps(
            {
                "agent": agent_id,
                "source": "test",
                "state": state,
                "timestamp": timestamp,
            }
        ),
        encoding="utf-8",
    )


def make_monitor(root: Path) -> tuple[StateMonitor, list, list]:
    agent_events: list[tuple[str, object]] = []
    resolved_events: list[object] = []
    monitor = StateMonitor(root / "sources", root / "state.json")
    monitor.agent_state_changed.connect(
        lambda agent_id, state: agent_events.append((agent_id, state))
    )
    monitor.resolved_state_changed.connect(resolved_events.append)
    return monitor, agent_events, resolved_events


def assert_latest(events: list[tuple[str, object]], agent_id: str, state: LifecycleState) -> None:
    matches = [event for event in events if event[0] == agent_id]
    assert matches and matches[-1][1].state == state


def main() -> None:
    app = QApplication.instance() or QApplication([])

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sources = root / "sources"
        monitor, agent_events, _ = make_monitor(root)

        write_source(sources, "claude", "idle")
        monitor.poll_once(NOW)
        assert_latest(agent_events, "claude", LifecycleState.IDLE)

        write_source(sources, "claude", "working", NOW + 1)
        monitor.poll_once(NOW + 1)
        assert_latest(agent_events, "claude", LifecycleState.WORKING)

        write_source(sources, "codex", "thinking", NOW + 2)
        monitor.poll_once(NOW + 2)
        assert_latest(agent_events, "codex", LifecycleState.THINKING)

        write_source(sources, "codex", "success", NOW + 3)
        monitor.poll_once(NOW + 3)
        assert_latest(agent_events, "codex", LifecycleState.SUCCESS)

        before_malformed = len(agent_events)
        (sources / "codex.json").write_text('{"state": "wor', encoding="utf-8")
        monitor.poll_once(NOW + 4)
        assert len(agent_events) == before_malformed

        (sources / "claude.json").unlink()
        before_missing = len(agent_events)
        monitor.poll_once(NOW + 5)
        assert len(agent_events) == before_missing

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sources = root / "sources"
        monitor, agent_events, resolved_events = make_monitor(root)
        write_source(sources, "claude", "working")
        monitor.poll_once(NOW)
        first_agent_count = len(agent_events)
        first_resolved_count = len(resolved_events)
        monitor.poll_once(NOW + 10)
        assert len(agent_events) == first_agent_count
        assert len(resolved_events) == first_resolved_count

        # A refreshed timestamp with the same lifecycle state is not a Dock change.
        write_source(sources, "claude", "working", NOW + 20)
        monitor.poll_once(NOW + 20)
        assert len(agent_events) == first_agent_count
        assert len(resolved_events) == first_resolved_count

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sources = root / "sources"
        monitor, agent_events, resolved_events = make_monitor(root)
        dock = AgentDock()
        pet = PetOverlay(PROJECT_DIR / "assets" / "animations", STATE_GIF)
        monitor.agent_state_changed.connect(
            lambda agent_id, state: dock.set_agent_state(agent_id, state.state.value)
        )
        monitor.resolved_state_changed.connect(
            lambda state: pet.apply_state(state.state.value)
        )

        dock.select_agent("claude")
        write_source(sources, "claude", "working", NOW)
        write_source(sources, "codex", "waiting", NOW + 1)
        monitor.poll_once(NOW + 1)
        app.processEvents()

        assert dock.selected_agent == "claude"
        assert dock.state_for("claude") == "working"
        assert dock.state_for("codex") == "waiting"
        assert dock.state_for("chatgpt") == "unavailable"
        assert_latest(agent_events, "claude", LifecycleState.WORKING)
        assert_latest(agent_events, "codex", LifecycleState.WAITING)
        assert resolved_events[-1].state == LifecycleState.WAITING
        assert resolved_events[-1].agent_id == "codex"
        assert pet.current_state == "waiting"

        persisted = json.loads((root / "state.json").read_text(encoding="utf-8"))
        assert persisted == resolved_events[-1].to_payload()
        assert set(persisted) == {"state", "agent", "source", "timestamp", "resolved_at"}
        dock.close()
        pet.shutdown()

    print("StateMonitor tests passed: 8 required cases + UI wiring.")


if __name__ == "__main__":
    main()
