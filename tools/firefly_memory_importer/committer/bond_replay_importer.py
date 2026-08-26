"""BondReplayImporter: replay BondCandidate -> BondState via the engine.

Stage 3 commit component. Never writes ``BondState`` fields directly — the only
write path is ``BondStateEngine.apply()`` (engine-owned). A dry-run folds the
pure ``apply_bond_rule`` over a snapshot to predict the result without writing.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.bond_rules import BondSignal, BondSignalType as RealType, apply_bond_rule
from core.bond_state import BondState

from ..analyzer.candidates import BondCandidate


@dataclass(frozen=True)
class BondMigrationPolicy:
    """Deterministic replay policy; TURN_COMPLETED is bulk-discounted."""

    turn_signal_batch: int = 10


class BondReplayImporter:
    """Convert bond candidates into a chronological signal script and replay it."""

    def __init__(self, policy: BondMigrationPolicy | None = None) -> None:
        self.policy = policy or BondMigrationPolicy()

    def build_script(self, candidates: list[BondCandidate]) -> list[BondSignal]:
        """Map candidates to real ``BondSignal``, sorted and bulk-discounted."""
        ordered = sorted(
            candidates, key=lambda c: min(ref.turn_seq for ref in c.evidence)
        )
        turn_count = sum(
            1 for c in ordered if c.signal_type.value == RealType.TURN_COMPLETED.value
        )
        allowed_turns = turn_count // self.policy.turn_signal_batch

        signals: list[BondSignal] = []
        for candidate in ordered:
            real_type = RealType(candidate.signal_type.value)
            if real_type is RealType.TURN_COMPLETED:
                if allowed_turns <= 0:
                    continue
                allowed_turns -= 1
                signals.append(BondSignal(real_type))
                continue
            if candidate.detail is not None:
                signals.append(BondSignal(real_type, candidate.detail))
            else:
                signals.append(BondSignal(real_type))
        return signals

    def dry_run(self, candidates: list[BondCandidate], current_state: BondState) -> BondState:
        """Predict the post-replay state without persisting anything."""
        state = current_state
        for signal in self.build_script(candidates):
            transition = apply_bond_rule(state, signal)
            state = BondState(
                phase=transition.phase,
                trust_level=transition.trust_level,
                familiarity_level=transition.familiarity_level,
                shared_milestones=transition.shared_milestones,
                pending_promises=transition.pending_promises,
            )
        return state

    def commit(self, engine, candidates: list[BondCandidate]) -> BondState:
        """Replay the script through the engine (the sole write path)."""
        for signal in self.build_script(candidates):
            engine.apply(signal)
        return engine.read()


__all__ = ["BondMigrationPolicy", "BondReplayImporter"]
