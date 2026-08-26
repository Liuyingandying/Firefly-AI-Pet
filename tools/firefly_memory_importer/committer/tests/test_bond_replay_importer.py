"""Tests for BondReplayImporter."""

from __future__ import annotations

from core.bond_rules import BondSignal, BondSignalType as RealType, apply_bond_rule
from core.bond_state import BondState

from tools.firefly_memory_importer.models import Role
from tools.firefly_memory_importer.analyzer.candidates import (
    Band,
    BondCandidate,
    BondSignalType,
    Disposition,
    SourceRef,
)
from tools.firefly_memory_importer.committer.bond_replay_importer import (
    BondMigrationPolicy,
    BondReplayImporter,
)


class FakeBondEngine:
    def __init__(self, state: BondState | None = None) -> None:
        self._state = state or BondState()

    def read(self) -> BondState:
        return self._state

    def apply(self, signal: BondSignal) -> BondState:
        transition = apply_bond_rule(self._state, signal)
        self._state = BondState(
            phase=transition.phase,
            trust_level=transition.trust_level,
            familiarity_level=transition.familiarity_level,
            shared_milestones=transition.shared_milestones,
            pending_promises=transition.pending_promises,
        )
        return self._state


def _bond(signal_type, detail=None, seq=0) -> BondCandidate:
    return BondCandidate(
        id="b-1",
        signal_type=signal_type,
        confidence=0.8,
        band=Band.HIGH,
        evidence=(SourceRef("c1", seq, Role.USER),),
        rule="bond.test",
        disposition=Disposition.AUTO_APPROVE,
        detail=detail,
    )


def test_build_script_maps_to_real_signals() -> None:
    importer = BondReplayImporter()

    script = importer.build_script(
        [_bond(BondSignalType.THANKED, seq=0), _bond(BondSignalType.SHARED_MILESTONE, "X", seq=1)]
    )

    assert len(script) == 2
    assert script[0].type is RealType.THANKED
    assert script[1].type is RealType.SHARED_MILESTONE
    assert script[1].detail == "X"


def test_turn_signal_batch_discount() -> None:
    importer = BondReplayImporter(BondMigrationPolicy(turn_signal_batch=2))

    candidates = [_bond(BondSignalType.TURN_COMPLETED, seq=i) for i in range(5)]

    script = importer.build_script(candidates)

    # floor(5 / 2) = 2 turn_completed signals
    assert len(script) == 2
    assert all(s.type is RealType.TURN_COMPLETED for s in script)


def test_dry_run_predicts_without_writing() -> None:
    importer = BondReplayImporter(BondMigrationPolicy(turn_signal_batch=1))
    state = BondState()

    after = importer.dry_run([_bond(BondSignalType.THANKED)], state)

    # one THANKED -> trust +0.02, original state untouched
    assert after.trust_level == 0.02
    assert state.trust_level == 0.0


def test_commit_replays_through_engine() -> None:
    importer = BondReplayImporter(BondMigrationPolicy(turn_signal_batch=1))
    engine = FakeBondEngine()

    after = importer.commit(engine, [_bond(BondSignalType.THANKED)])

    assert after.trust_level == 0.02
    assert engine.read().trust_level == 0.02
