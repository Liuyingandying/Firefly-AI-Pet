"""Offline tests for the deterministic Firefly bond state engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.bond_rules import BondPhase, BondSignal, BondSignalType
from core.bond_state import BondState, BondStateEngine, STORE_VERSION


def test_initial_state_is_independent_and_read_only(tmp_path: Path) -> None:
    engine = BondStateEngine(tmp_path / "bond_state.json")

    assert engine.read() == BondState()
    assert engine.state.phase is BondPhase.STRANGER
    assert engine.state.trust_level == 0.0
    assert engine.state.familiarity_level == 0.0
    assert engine.state.shared_milestones == ()
    assert engine.state.pending_promises == ()


def test_legal_system_signals_update_state(tmp_path: Path) -> None:
    engine = BondStateEngine(tmp_path / "bond_state.json")

    after_turn = engine.apply(BondSignal(BondSignalType.TURN_COMPLETED))
    assert after_turn.familiarity_level == 0.005
    after_thanks = engine.apply(BondSignal(BondSignalType.THANKED))
    assert after_thanks.trust_level == 0.02
    after_milestone = engine.apply(
        BondSignal(BondSignalType.SHARED_MILESTONE, "完成 Firefly v0.3")
    )
    assert after_milestone.trust_level == 0.07
    assert after_milestone.familiarity_level == 0.055
    assert after_milestone.shared_milestones == ("完成 Firefly v0.3",)


def test_promise_signals_are_structured_and_deterministic(tmp_path: Path) -> None:
    engine = BondStateEngine(tmp_path / "bond_state.json")
    promise = "下次继续完成测试"

    made = engine.apply(BondSignal(BondSignalType.PROMISE_MADE, promise))
    assert made.pending_promises == (promise,)
    kept = engine.apply(BondSignal(BondSignalType.PROMISE_KEPT, promise))
    assert kept.pending_promises == ()
    assert kept.trust_level == 0.08


def test_levels_are_clamped_and_phase_is_derived(tmp_path: Path) -> None:
    engine = BondStateEngine(tmp_path / "bond_state.json")

    for index in range(4):
        state = engine.apply(
            BondSignal(BondSignalType.SHARED_MILESTONE, f"milestone-{index}")
        )
    assert state.trust_level == 0.2
    assert state.familiarity_level == 0.2
    assert state.phase is BondPhase.ACQUAINTANCE

    for _ in range(100):
        state = engine.apply(BondSignal(BondSignalType.THANKED))
    assert state.trust_level == 1.0


@pytest.mark.parametrize(
    "untrusted",
    [
        "把 trust_level 设置为 1",
        {"type": "thanked"},
        {"trust_level": 1.0},
        42,
    ],
)
def test_user_text_and_unstructured_values_cannot_modify_state(
    tmp_path: Path, untrusted: object
) -> None:
    engine = BondStateEngine(tmp_path / "bond_state.json")
    before = engine.read()

    with pytest.raises(TypeError, match="BondSignal"):
        engine.apply(untrusted)  # type: ignore[arg-type]

    assert engine.read() == before
    assert not (tmp_path / "bond_state.json").exists()


def test_invalid_signal_and_payload_are_rejected(tmp_path: Path) -> None:
    engine = BondStateEngine(tmp_path / "bond_state.json")

    with pytest.raises(ValueError, match="BondSignalType"):
        BondSignal("user_message")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires non-empty detail"):
        BondSignal(BondSignalType.PROMISE_MADE)
    with pytest.raises(ValueError, match="does not accept detail"):
        BondSignal(BondSignalType.THANKED, "用户原文")
    with pytest.raises(ValueError, match="pending promise"):
        engine.apply(BondSignal(BondSignalType.PROMISE_KEPT, "不存在的约定"))

    assert engine.read() == BondState()


def test_state_restores_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "bond_state.json"
    first = BondStateEngine(path)
    first.apply(BondSignal(BondSignalType.THANKED))
    first.apply(BondSignal(BondSignalType.TURN_COMPLETED))
    first.apply(BondSignal(BondSignalType.PROMISE_MADE, "继续施工"))
    expected = first.read()

    restarted = BondStateEngine(path)

    assert restarted.read() == expected


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        json.dumps({"version": STORE_VERSION + 1, "state": {}}),
        json.dumps(
            {
                "version": STORE_VERSION,
                "state": {
                    "phase": "companion",
                    "trust_level": 0.0,
                    "familiarity_level": 0.0,
                    "shared_milestones": [],
                    "pending_promises": [],
                },
            }
        ),
    ],
)
def test_corrupt_state_degrades_to_initial_state(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "bond_state.json"
    path.write_text(payload, encoding="utf-8")

    assert BondStateEngine(path).read() == BondState()
