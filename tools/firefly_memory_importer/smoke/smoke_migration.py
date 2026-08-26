"""Stage 4-A small-scale migration with dry-run, audit, and rollback.

This module intentionally reuses existing production boundaries: memory writes
go through ``MemoryService``, conversations through ``ConversationStore``, and
bond changes through ``BondStateEngine.apply(BondSignal)``. It performs no LLM
analysis and never reads or writes character YAML content.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from core.bond_rules import BondSignal, BondSignalType
from core.bond_state import BondState, BondStateEngine
from core.conversation_store import ConversationStore
from memory.records import MemoryCategory

from ..models import Conversation, ParsedHistory, Role, Turn


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_AUDIT_DIR = PROJECT_DIR / "runtime" / "companion" / "migration" / "smoke"
DEFAULT_CHARACTER_DIR = PROJECT_DIR / "character"
SAFE_MEMORY_CATEGORIES = frozenset(
    {
        MemoryCategory.USER_FACT.value,
        MemoryCategory.PREFERENCE.value,
        MemoryCategory.PROJECT.value,
        MemoryCategory.SHARED_EXPERIENCE.value,
    }
)
ALLOWED_BOND_SIGNALS = frozenset(
    {BondSignalType.TURN_COMPLETED.value, BondSignalType.THANKED.value}
)


class SmokeMigrationError(RuntimeError):
    """Raised when smoke input or a reversible commit is invalid."""


class SmokeMemoryService(Protocol):
    def import_migrated(
        self,
        content: str,
        *,
        category: MemoryCategory | str,
        trigger: str,
    ) -> Any: ...

    def delete(self, record_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class SmokePolicy:
    memory_limit: int = 5
    conversation_turns: int = 40
    turn_completed_limit: int = 2
    thanked_limit: int = 2
    bond_ceiling: float = 0.7

    def __post_init__(self) -> None:
        if not 1 <= self.memory_limit <= 5:
            raise ValueError("memory_limit must be between 1 and 5")
        if not 20 <= self.conversation_turns <= 50:
            raise ValueError("conversation_turns must be between 20 and 50")
        if not 0 <= self.turn_completed_limit <= 5:
            raise ValueError("turn_completed_limit must be between 0 and 5")
        if not 0 <= self.thanked_limit <= 5:
            raise ValueError("thanked_limit must be between 0 and 5")
        if not 0.0 <= self.bond_ceiling <= 1.0:
            raise ValueError("bond_ceiling must be between 0 and 1")


class _NonPersistingBondEngine(BondStateEngine):
    def __init__(self, initial_state: BondState) -> None:
        self._initial_state = initial_state
        super().__init__(path=Path("<smoke-migration-no-write>"))

    def _load(self) -> BondState:
        return self._initial_state

    def _persist(self, state: BondState) -> None:
        return None


class MigrationSmokeCommit:
    """Select and apply one tightly bounded, reversible migration sample."""

    def __init__(
        self,
        memory_service: SmokeMemoryService,
        conversation_store: ConversationStore,
        bond_engine: BondStateEngine,
        *,
        audit_dir: str | Path = DEFAULT_AUDIT_DIR,
        policy: SmokePolicy | None = None,
        character_yaml_paths: Sequence[str | Path] | None = None,
    ) -> None:
        if not callable(getattr(memory_service, "import_migrated", None)):
            raise TypeError("memory_service must provide import_migrated()")
        if not callable(getattr(memory_service, "delete", None)):
            raise TypeError("memory_service must provide delete()")
        if not isinstance(conversation_store, ConversationStore):
            raise TypeError("conversation_store must be a ConversationStore")
        if not isinstance(bond_engine, BondStateEngine):
            raise TypeError("bond_engine must be a BondStateEngine")
        self.memory_service = memory_service
        self.conversation_store = conversation_store
        self.bond_engine = bond_engine
        self.audit_dir = Path(audit_dir)
        self.policy = policy or SmokePolicy()
        self.character_yaml_paths = (
            tuple(Path(path) for path in character_yaml_paths)
            if character_yaml_paths is not None
            else tuple(sorted(DEFAULT_CHARACTER_DIR.rglob("*.yaml")))
        )

    def dry_run(
        self,
        parsed_history: ParsedHistory | str | Path | Mapping[str, Any],
        migration_preview: str | Path | Mapping[str, Any],
        *,
        accepted_memory_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        history = _load_history(parsed_history)
        preview = _load_mapping(migration_preview, "migration preview")
        selection = self._select(history, preview, set(accepted_memory_ids))
        before = self.bond_engine.read()
        predicted_after, selected_bond = self._predict_bond(before, selection["bond"])
        return {
            "status": "dry_run",
            "source": history.source,
            "source_file": history.source_file,
            "memory": {
                "record_count": len(selection["memory"]),
                "candidate_ids": [item["id"] for item in selection["memory"]],
                "records": [
                    {"category": item["category"], "content": item["content"]}
                    for item in selection["memory"]
                ],
            },
            "conversation": {
                "turn_count": len(selection["conversation"]),
                "session_id": self.conversation_store.active_session_id,
            },
            "bond": {
                "state_before": before.to_dict(),
                "predicted_state_after": predicted_after.to_dict(),
                "signals": [
                    {"type": signal.type.value, "detail": signal.detail}
                    for signal in selected_bond
                ],
            },
            "safety": {
                "memory_limit": self.policy.memory_limit,
                "conversation_turn_limit": self.policy.conversation_turns,
                "bond_ceiling": self.policy.bond_ceiling,
                "character_yaml_unchanged": True,
                "llm_analysis_used": False,
                "memory_write_path": "MemoryService.import_migrated",
            },
        }

    def commit(
        self,
        parsed_history: ParsedHistory | str | Path | Mapping[str, Any],
        migration_preview: str | Path | Mapping[str, Any],
        *,
        accepted_memory_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        history = _load_history(parsed_history)
        preview = _load_mapping(migration_preview, "migration preview")
        selection = self._select(history, preview, set(accepted_memory_ids))
        before = self.bond_engine.read()
        predicted_after, selected_bond = self._predict_bond(before, selection["bond"])
        run_id = _new_run_id()
        snapshot_dir = self.audit_dir / "snapshots" / run_id
        audit_path = self.audit_dir / f"{run_id}.json"
        yaml_before = _hash_files(self.character_yaml_paths)
        snapshots = self._capture_snapshots(snapshot_dir)
        record_ids: list[str] = []
        audit: dict[str, Any] = {
            "run_id": run_id,
            "status": "committing",
            "source": history.source,
            "source_file": history.source_file,
            "created_ts": time.time_ns() // 1_000_000,
            "snapshot_dir": str(snapshot_dir),
            "snapshots": snapshots,
            "memory": {"candidate_ids": [], "record_ids": []},
            "conversation": {
                "session_id": self.conversation_store.active_session_id,
                "turn_count": len(selection["conversation"]),
            },
            "bond": {
                "state_before": before.to_dict(),
                "predicted_state_after": predicted_after.to_dict(),
                "signals": [
                    {"type": signal.type.value, "detail": signal.detail}
                    for signal in selected_bond
                ],
            },
        }
        self._write_audit(audit_path, audit)
        try:
            for item in selection["memory"]:
                try:
                    record = self.memory_service.import_migrated(
                        item["content"],
                        category=item["category"],
                        trigger=f"migration:{run_id}",
                    )
                except Exception as exc:
                    partial = getattr(exc, "record", None)
                    partial_id = getattr(partial, "id", None)
                    if isinstance(partial_id, str):
                        record_ids.append(partial_id)
                    raise
                record_id = getattr(record, "id", None)
                if not isinstance(record_id, str) or not record_id:
                    raise SmokeMigrationError("MemoryService returned no record ID")
                record_ids.append(record_id)
                audit["memory"]["candidate_ids"].append(item["id"])
                audit["memory"]["record_ids"] = list(record_ids)
                self._write_audit(audit_path, audit)

            self._replace_conversation(selection["conversation"])
            state_after = before
            for signal in selected_bond:
                state_after = self.bond_engine.apply(signal)
            if state_after != predicted_after:
                raise SmokeMigrationError("bond prediction did not match committed state")
            if _hash_files(self.character_yaml_paths) != yaml_before:
                raise SmokeMigrationError("character YAML changed during smoke commit")

            audit["status"] = "committed"
            audit["memory"]["record_ids"] = list(record_ids)
            audit["bond"]["state_after"] = state_after.to_dict()
            audit["safety"] = {
                "character_yaml_unchanged": True,
                "memory_write_path": "MemoryService.import_migrated",
                "llm_analysis_used": False,
                "rollback_available": True,
            }
            self._write_audit(audit_path, audit)
            return audit
        except Exception as exc:
            audit["memory"]["record_ids"] = list(record_ids)
            audit["error"] = f"{type(exc).__name__}: {exc}"
            self._rollback_audit(audit, audit_path)
            raise

    def rollback(self, run_id: str) -> dict[str, Any]:
        if not isinstance(run_id, str) or not run_id.startswith("smoke-"):
            raise SmokeMigrationError("run_id must be a smoke migration ID")
        audit_path = self.audit_dir / f"{run_id}.json"
        audit = _load_mapping(audit_path, "smoke audit")
        if audit.get("run_id") != run_id:
            raise SmokeMigrationError("audit run_id mismatch")
        mutable = dict(audit)
        self._rollback_audit(mutable, audit_path)
        return mutable

    def _select(
        self,
        history: ParsedHistory,
        preview: Mapping[str, Any],
        accepted_memory_ids: set[str],
    ) -> dict[str, list[Any]]:
        groups = preview.get("groups")
        if not isinstance(groups, Mapping):
            raise SmokeMigrationError("migration preview is missing groups")
        raw_memory = groups.get("memory")
        raw_bond = groups.get("bond")
        if not isinstance(raw_memory, list) or not isinstance(raw_bond, list):
            raise SmokeMigrationError("migration preview memory/bond groups must be lists")

        selected_memory: list[Mapping[str, Any]] = []
        for raw in raw_memory:
            item = _candidate(raw, "memory")
            manually_accepted = item["id"] in accepted_memory_ids
            is_accepted = (
                item.get("disposition") == "auto_approve"
                or item.get("review") == "accepted"
                or manually_accepted
            )
            flags = item.get("flags", [])
            risk_flags_allowed = (
                isinstance(flags, list)
                and (
                    not flags
                    or (manually_accepted and set(flags) <= {"roleplay"})
                )
            )
            if (
                is_accepted
                and item.get("category") in SAFE_MEMORY_CATEGORIES
                and risk_flags_allowed
                and _safe_memory_content(item.get("content"))
            ):
                selected_memory.append(item)
            if len(selected_memory) >= self.policy.memory_limit:
                break

        turns = sorted(
            (turn for conversation in history.conversations for turn in conversation.turns),
            key=lambda turn: turn.seq,
        )
        selected_turns = [
            turn
            for turn in turns
            if turn.role in {Role.USER, Role.ASSISTANT}
            and turn.content.strip()
            and turn.content != "[image]"
        ][-self.policy.conversation_turns :]

        selected_bond: list[Mapping[str, Any]] = []
        per_type: dict[str, int] = {value: 0 for value in ALLOWED_BOND_SIGNALS}
        limits = {
            BondSignalType.TURN_COMPLETED.value: self.policy.turn_completed_limit,
            BondSignalType.THANKED.value: self.policy.thanked_limit,
        }
        ordered_bond = sorted(
            (_candidate(raw, "bond") for raw in raw_bond),
            key=_candidate_turn_seq,
            reverse=True,
        )
        for item in ordered_bond:
            signal_type = item.get("signal_type")
            if signal_type not in ALLOWED_BOND_SIGNALS:
                continue
            accepted = item.get("disposition") == "auto_approve" or item.get("review") == "accepted"
            if accepted and per_type[signal_type] < limits[signal_type]:
                selected_bond.append(item)
                per_type[signal_type] += 1

        return {
            "memory": selected_memory,
            "conversation": selected_turns,
            "bond": list(reversed(selected_bond)),
        }

    def _predict_bond(
        self, before: BondState, candidates: list[Mapping[str, Any]]
    ) -> tuple[BondState, list[BondSignal]]:
        engine = _NonPersistingBondEngine(before)
        selected: list[BondSignal] = []
        ceilings = (
            max(before.trust_level, self.policy.bond_ceiling),
            max(before.familiarity_level, self.policy.bond_ceiling),
        )
        for item in candidates:
            signal = BondSignal(BondSignalType(item["signal_type"]))
            prior_state = engine.read()
            candidate_state = engine.apply(signal)
            if (
                candidate_state.trust_level <= ceilings[0]
                and candidate_state.familiarity_level <= ceilings[1]
            ):
                selected.append(signal)
            else:
                engine = _NonPersistingBondEngine(prior_state)
        return engine.read(), selected

    def _replace_conversation(self, turns: list[Turn]) -> None:
        self.conversation_store.clear()
        fallback_ts = max(1, time.time_ns() // 1_000_000 - len(turns))
        previous_ts = 0
        for index, turn in enumerate(turns):
            timestamp = max(previous_ts + 1, _turn_timestamp(turn.ts, fallback_ts + index))
            self.conversation_store.append_turn(
                turn.role.value,
                turn.content,
                timestamp_ms=timestamp,
            )
            previous_ts = timestamp

    def _capture_snapshots(self, snapshot_dir: Path) -> dict[str, dict[str, Any]]:
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        return {
            "conversation": _snapshot_file(self.conversation_store.path, snapshot_dir),
            "bond": _snapshot_file(self.bond_engine.path, snapshot_dir),
        }

    def _rollback_audit(self, audit: dict[str, Any], audit_path: Path) -> None:
        errors: list[str] = []
        for record_id in reversed(audit.get("memory", {}).get("record_ids", [])):
            try:
                self.memory_service.delete(record_id)
            except Exception as exc:
                errors.append(f"memory {record_id}: {exc}")
        snapshots = audit.get("snapshots", {})
        for name, target in (
            ("conversation", self.conversation_store.path),
            ("bond", self.bond_engine.path),
        ):
            try:
                _restore_snapshot(snapshots.get(name), target)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        audit["status"] = "rollback_failed" if errors else "rolled_back"
        audit["rolled_back_ts"] = time.time_ns() // 1_000_000
        audit["rollback_errors"] = errors
        self._write_audit(audit_path, audit)
        if errors:
            raise SmokeMigrationError("smoke rollback was incomplete: " + "; ".join(errors))

    @staticmethod
    def _write_audit(path: Path, audit: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(audit, ensure_ascii=False, indent=2) + "\n")


def _load_history(value: ParsedHistory | str | Path | Mapping[str, Any]) -> ParsedHistory:
    if isinstance(value, ParsedHistory):
        return value
    data = _load_mapping(value, "parsed history")
    raw_conversations = data.get("conversations")
    if not isinstance(raw_conversations, list):
        raise SmokeMigrationError("parsed history conversations must be a list")
    conversations: list[Conversation] = []
    for raw_conversation in raw_conversations:
        if not isinstance(raw_conversation, Mapping):
            raise SmokeMigrationError("parsed conversation must be an object")
        raw_turns = raw_conversation.get("turns")
        if not isinstance(raw_turns, list):
            raise SmokeMigrationError("parsed conversation turns must be a list")
        turns = [
            Turn(
                role=Role(raw["role"]),
                content=raw["content"],
                ts=raw.get("ts"),
                seq=raw["seq"],
                message_id=raw.get("message_id"),
                content_type=raw.get("content_type"),
            )
            for raw in raw_turns
            if isinstance(raw, Mapping)
        ]
        conversations.append(
            Conversation(id=str(raw_conversation.get("id", "")), title=raw_conversation.get("title"), turns=turns)
        )
    return ParsedHistory(
        source_file=str(data.get("source_file", "")),
        source=str(data.get("source", "")),
        conversations=conversations,
    )


def _load_mapping(value: str | Path | Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        data = json.loads(Path(value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeMigrationError(f"cannot load {label}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise SmokeMigrationError(f"{label} must be a JSON object")
    return data


def _candidate(value: Any, expected_kind: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or value.get("kind") != expected_kind:
        raise SmokeMigrationError(f"invalid {expected_kind} candidate")
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise SmokeMigrationError(f"{expected_kind} candidate is missing id")
    return value


def _candidate_turn_seq(candidate: Mapping[str, Any]) -> int:
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list):
        return -1
    values = [item.get("turn_seq") for item in evidence if isinstance(item, Mapping)]
    integers = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    return max(integers, default=-1)


def _safe_memory_content(value: Any) -> bool:
    if not isinstance(value, str) or len(value.strip()) < 2:
        return False
    text = value.strip()
    question_markers = ("?", "？", "什么", "哪个", "哪所", "谁", "是否", "吗")
    return not any(marker in text for marker in question_markers)


def _turn_timestamp(value: str | None, fallback: int) -> int:
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        normalized = value.strip().replace("Z", "+00:00")
        return max(1, int(datetime.fromisoformat(normalized).timestamp() * 1000))
    except (ValueError, OverflowError):
        return fallback


def _snapshot_file(path: Path, snapshot_dir: Path) -> dict[str, Any]:
    existed = path.exists()
    snapshot_path = snapshot_dir / path.name
    if existed:
        snapshot_path.write_bytes(path.read_bytes())
    return {"target": str(path), "existed": existed, "snapshot": str(snapshot_path)}


def _restore_snapshot(metadata: Any, target: Path) -> None:
    if not isinstance(metadata, Mapping) or metadata.get("target") != str(target):
        raise SmokeMigrationError(f"invalid snapshot metadata for {target}")
    if metadata.get("existed") is True:
        snapshot_path = Path(str(metadata.get("snapshot")))
        if not snapshot_path.is_file():
            raise SmokeMigrationError(f"snapshot is missing: {snapshot_path}")
        _atomic_write_bytes(target, snapshot_path.read_bytes())
    else:
        target.unlink(missing_ok=True)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _hash_files(paths: Sequence[Path]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for path in paths:
        result[str(path.resolve())] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        )
    return result


def _new_run_id() -> str:
    return f"smoke-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


__all__ = [
    "DEFAULT_AUDIT_DIR",
    "MigrationSmokeCommit",
    "SmokeMigrationError",
    "SmokePolicy",
]
