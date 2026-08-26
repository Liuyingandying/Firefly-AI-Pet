"""Persistent, deterministic relationship state for Firefly AI Pet."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from core.bond_rules import (
    BondPhase,
    BondSignal,
    apply_bond_rule,
    phase_for,
)


STORE_VERSION = 1
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BOND_STATE_PATH = PROJECT_DIR / "runtime" / "companion" / "bond_state.json"


@dataclass(frozen=True, slots=True)
class BondState:
    """Immutable snapshot; only BondStateEngine can persist a successor."""

    phase: BondPhase = BondPhase.STRANGER
    trust_level: float = 0.0
    familiarity_level: float = 0.0
    shared_milestones: tuple[str, ...] = ()
    pending_promises: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            normalized_phase = BondPhase(self.phase)
        except (TypeError, ValueError) as exc:
            raise ValueError("phase must be a valid BondPhase") from exc
        trust = _level(self.trust_level, "trust_level")
        familiarity = _level(self.familiarity_level, "familiarity_level")
        milestones = _string_tuple(self.shared_milestones, "shared_milestones")
        promises = _string_tuple(self.pending_promises, "pending_promises")
        if normalized_phase is not phase_for(trust, familiarity):
            raise ValueError("phase must match trust and familiarity levels")
        object.__setattr__(self, "phase", normalized_phase)
        object.__setattr__(self, "trust_level", trust)
        object.__setattr__(self, "familiarity_level", familiarity)
        object.__setattr__(self, "shared_milestones", milestones)
        object.__setattr__(self, "pending_promises", promises)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "trust_level": self.trust_level,
            "familiarity_level": self.familiarity_level,
            "shared_milestones": list(self.shared_milestones),
            "pending_promises": list(self.pending_promises),
        }

    @classmethod
    def from_dict(cls, data: Any) -> BondState:
        if not isinstance(data, Mapping):
            raise ValueError("bond state data must be a mapping")
        required = {
            "phase",
            "trust_level",
            "familiarity_level",
            "shared_milestones",
            "pending_promises",
        }
        if not required.issubset(data):
            raise ValueError("bond state data is missing required fields")
        return cls(
            phase=data["phase"],
            trust_level=data["trust_level"],
            familiarity_level=data["familiarity_level"],
            shared_milestones=data["shared_milestones"],
            pending_promises=data["pending_promises"],
        )


class BondStateEngine:
    """The sole transition and persistence owner for BondState."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_BOND_STATE_PATH
        self._lock = RLock()
        self._state = self._load()

    @property
    def state(self) -> BondState:
        return self.read()

    def read(self) -> BondState:
        with self._lock:
            return self._state

    def apply(self, signal: BondSignal) -> BondState:
        """Apply one trusted system signal; reject strings and mappings."""
        if not isinstance(signal, BondSignal):
            raise TypeError("signal must be a BondSignal")
        with self._lock:
            transition = apply_bond_rule(self._state, signal)
            next_state = BondState(
                phase=transition.phase,
                trust_level=transition.trust_level,
                familiarity_level=transition.familiarity_level,
                shared_milestones=transition.shared_milestones,
                pending_promises=transition.pending_promises,
            )
            self._persist(next_state)
            self._state = next_state
            return next_state

    def reset(self) -> BondState:
        with self._lock:
            initial = BondState()
            self._persist(initial)
            self._state = initial
            return initial

    def _load(self) -> BondState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return BondState()
        if not isinstance(data, Mapping):
            return BondState()
        version = data.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or version > STORE_VERSION
        ):
            return BondState()
        try:
            return BondState.from_dict(data.get("state"))
        except (TypeError, ValueError):
            return BondState()

    def _persist(self, state: BondState) -> None:
        payload = {"version": STORE_VERSION, "state": state.to_dict()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._atomic_replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _atomic_replace(src: Path, dst: Path) -> None:
        """Atomic replace with Windows WinError 5 retry.

        On Windows, os.replace can fail with PermissionError [WinError 5]
        when the destination file is briefly locked by another process or
        the OS (antivirus, indexing, etc.). Retry a few times with short
        backoff before giving up.
        """
        import time

        max_retries = 5
        for attempt in range(max_retries):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                else:
                    raise


def _level(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number between 0 and 1")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{name} must be a finite number between 0 and 1")
    return normalized


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of non-empty strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} must contain only non-empty strings")
        text = item.strip()
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)


__all__ = [
    "DEFAULT_BOND_STATE_PATH",
    "STORE_VERSION",
    "BondState",
    "BondStateEngine",
]
