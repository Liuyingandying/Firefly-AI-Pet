"""Complete, zero-write validation of ``reviewed_migration.json``.

The validator deliberately does not construct a repository, service, vector
adapter, or commit orchestrator. Memory and style records are only materialized
as validated in-memory predictions. Bond candidates are replayed through a real
``BondStateEngine.apply()`` whose persistence hook is disabled.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.bond_rules import BondSignal, BondSignalType
from core.bond_state import BondState, BondStateEngine
from memory.records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CHARACTER_DIR = PROJECT_DIR / "character"


class MigrationValidationError(ValueError):
    """Raised when reviewed migration input is incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class MemoryValidation:
    accepted_count: int
    predicted_records: list[dict[str, Any]]
    rejected_count: int


@dataclass(frozen=True, slots=True)
class BondValidation:
    state_before: dict[str, Any]
    predicted_state_after: dict[str, Any]
    signal_summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StyleValidation:
    accepted_count: int
    predicted_profile: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SafetyChecks:
    character_yaml_unchanged: bool
    repository_write_count: int
    bond_persisted: bool
    rollback_available: bool


@dataclass(frozen=True, slots=True)
class MigrationValidationReport:
    run_id: str
    source: str
    memory: MemoryValidation
    bond: BondValidation
    style: StyleValidation
    safety_checks: SafetyChecks

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible report mapping."""
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize the complete report without writing it to disk."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class _NonPersistingBondStateEngine(BondStateEngine):
    """Real transition engine with an injected initial state and no I/O."""

    def __init__(self, initial_state: BondState) -> None:
        self._initial_state = initial_state
        self.persist_attempts = 0
        super().__init__(path=Path("<migration-validation-no-write>"))

    def _load(self) -> BondState:
        return self._initial_state

    def _persist(self, state: BondState) -> None:
        self.persist_attempts += 1


def validate_reviewed_migration(
    reviewed_migration: str | Path | Mapping[str, Any],
    *,
    bond_state: BondState | None = None,
    character_yaml_paths: Sequence[str | Path] | None = None,
) -> MigrationValidationReport:
    """Validate reviewed candidates and predict their effects with zero writes.

    ``bond_state`` defaults to the current state read by ``BondStateEngine``.
    Character YAML files are hashed before and after validation. The returned
    report itself is in memory; callers may explicitly persist it elsewhere.
    """
    payload = _load_payload(reviewed_migration)
    run_id = _required_text(payload, "run_id")
    source = _required_text(payload, "source")
    _required_text(payload, "source_file")
    rule_version = payload.get("rule_version")
    if isinstance(rule_version, bool) or not isinstance(rule_version, int) or rule_version < 1:
        raise MigrationValidationError("root.rule_version must be a positive integer")
    memory_items = _required_list(payload, "memory")
    bond_items = _required_list(payload, "bond")
    style_items = _required_list(payload, "style")
    rejected_items = _required_list(payload, "rejected")
    if not isinstance(payload.get("decisions"), Mapping):
        raise MigrationValidationError("root.decisions must be an object")

    yaml_paths = _character_yaml_paths(character_yaml_paths)
    yaml_before = _file_hashes(yaml_paths)

    predicted_records = [
        _predicted_record(item, run_id, path=f"memory[{index}]")
        for index, item in enumerate(memory_items)
    ]
    rejected_memory_count = sum(
        1
        for index, item in enumerate(rejected_items)
        if _rejected_kind(item, index) == "memory"
    )

    state_before = bond_state or BondStateEngine().read()
    if not isinstance(state_before, BondState):
        raise MigrationValidationError("bond_state must be a BondState")
    engine = _NonPersistingBondStateEngine(state_before)
    signal_counts: Counter[str] = Counter()
    for index, item in enumerate(bond_items):
        signal = _bond_signal(item, path=f"bond[{index}]")
        engine.apply(signal)
        signal_counts[signal.type.value] += 1
    state_after = engine.read()

    profile_entries: list[dict[str, Any]] = []
    preference_records: list[dict[str, Any]] = []
    for index, item in enumerate(style_items):
        path = f"style[{index}]"
        entry = _style_entry(item, path=path)
        profile_entries.append(entry)
        preference_records.append(
            _create_record(
                category=MemoryCategory.PREFERENCE,
                content=f"{entry['dimension']}: {entry['content']}",
                run_id=run_id,
            )
        )

    yaml_after = _file_hashes(yaml_paths)
    return MigrationValidationReport(
        run_id=run_id,
        source=source,
        memory=MemoryValidation(
            accepted_count=len(memory_items),
            predicted_records=predicted_records,
            rejected_count=rejected_memory_count,
        ),
        bond=BondValidation(
            state_before=state_before.to_dict(),
            predicted_state_after=state_after.to_dict(),
            signal_summary={
                "accepted_count": len(bond_items),
                "applied_count": sum(signal_counts.values()),
                "by_type": dict(sorted(signal_counts.items())),
            },
        ),
        style=StyleValidation(
            accepted_count=len(style_items),
            predicted_profile={
                "run_id": run_id,
                "review_only": True,
                "entries": profile_entries,
                "preference_records": preference_records,
            },
        ),
        safety_checks=SafetyChecks(
            character_yaml_unchanged=yaml_before == yaml_after,
            repository_write_count=0,
            bond_persisted=False,
            rollback_available=True,
        ),
    )


def _load_payload(value: str | Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        loaded = json.loads(Path(value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationValidationError(f"cannot load reviewed migration: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise MigrationValidationError("reviewed migration must be a JSON object")
    return loaded


def _required_text(data: Mapping[str, Any], key: str, *, path: str = "root") -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MigrationValidationError(f"{path}.{key} must be a non-empty string")
    return value.strip()


def _required_list(data: Mapping[str, Any], key: str) -> list[Any]:
    if key not in data:
        raise MigrationValidationError(f"root is missing required field: {key}")
    value = data[key]
    if not isinstance(value, list):
        raise MigrationValidationError(f"root.{key} must be a list")
    return value


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MigrationValidationError(f"{path} must be an object")
    return value


def _predicted_record(item: Any, run_id: str, *, path: str) -> dict[str, Any]:
    candidate = _mapping(item, path)
    _validate_accepted_candidate(candidate, path=path, expected_kind="memory")
    category_text = _required_text(candidate, "category", path=path)
    content = _required_text(candidate, "content", path=path)
    try:
        category = MemoryCategory(category_text)
    except ValueError as exc:
        raise MigrationValidationError(f"{path}.category is invalid") from exc
    return _create_record(category=category, content=content, run_id=run_id)


def _create_record(
    *, category: MemoryCategory, content: str, run_id: str
) -> dict[str, Any]:
    return MemoryRecord.create(
        category=category,
        content=content,
        trigger=f"migration:{run_id}",
        source=MemorySource.MIGRATED,
        permission=WritePolicy.AUTO,
    ).to_dict()


def _bond_signal(item: Any, *, path: str) -> BondSignal:
    candidate = _mapping(item, path)
    _validate_accepted_candidate(candidate, path=path, expected_kind="bond")
    signal_type_text = _required_text(candidate, "signal_type", path=path)
    try:
        signal_type = BondSignalType(signal_type_text)
    except ValueError as exc:
        raise MigrationValidationError(f"{path}.signal_type is invalid") from exc
    detail = candidate.get("detail")
    try:
        return BondSignal(signal_type, detail)
    except (TypeError, ValueError) as exc:
        raise MigrationValidationError(f"{path} is not a valid BondSignal: {exc}") from exc


def _style_entry(item: Any, *, path: str) -> dict[str, Any]:
    candidate = _mapping(item, path)
    _validate_accepted_candidate(candidate, path=path, expected_kind="style")
    dimension = _required_text(candidate, "dimension", path=path)
    if dimension not in {"nickname", "tone", "verbosity", "boundary"}:
        raise MigrationValidationError(f"{path}.dimension is invalid")
    content = _required_text(candidate, "content", path=path)
    rule = _required_text(candidate, "rule", path=path)
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list):
        raise MigrationValidationError(f"{path}.evidence must be a list")
    return {
        "dimension": dimension,
        "content": content,
        "rule": rule,
        "evidence": evidence,
        "review_only": True,
    }


def _validate_accepted_candidate(
    candidate: Mapping[str, Any], *, path: str, expected_kind: str
) -> None:
    _required_text(candidate, "id", path=path)
    kind = _required_text(candidate, "kind", path=path)
    if kind != expected_kind:
        raise MigrationValidationError(f"{path}.kind must be {expected_kind!r}")
    if not isinstance(candidate.get("evidence"), list) or not candidate["evidence"]:
        raise MigrationValidationError(f"{path}.evidence must be a non-empty list")


def _rejected_kind(item: Any, index: int) -> str | None:
    candidate = _mapping(item, f"rejected[{index}]")
    kind = candidate.get("kind")
    if kind is not None and kind not in {"memory", "bond", "style"}:
        raise MigrationValidationError(f"rejected[{index}].kind is invalid")
    return kind


def _character_yaml_paths(paths: Sequence[str | Path] | None) -> tuple[Path, ...]:
    if paths is None:
        return tuple(sorted(DEFAULT_CHARACTER_DIR.rglob("*.yaml")))
    return tuple(Path(path) for path in paths)


def _file_hashes(paths: Sequence[Path]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for path in paths:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            digest = None
        except OSError as exc:
            raise MigrationValidationError(f"cannot read character yaml {path}: {exc}") from exc
        result[str(path.resolve())] = digest
    return result


__all__ = [
    "MigrationValidationError",
    "MigrationValidationReport",
    "validate_reviewed_migration",
]
