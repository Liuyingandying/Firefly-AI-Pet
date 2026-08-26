"""Stage 4-A bounded real-write smoke migration tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.bond_rules import BondSignal, BondSignalType
from core.bond_state import BondStateEngine
from core.conversation_store import ConversationStore
from ui.character_conversation_runner import CharacterConversationRunner
from tools.firefly_memory_importer.models import Conversation, ParsedHistory, Role, Turn
from tools.firefly_memory_importer.smoke.smoke_migration import (
    MigrationSmokeCommit,
    SmokePolicy,
)


class FakeMemoryService:
    def __init__(self) -> None:
        self.records: dict[str, SimpleNamespace] = {}
        self.calls: list[dict] = []

    def import_migrated(self, content, **kwargs):
        record = SimpleNamespace(id=f"record-{len(self.records) + 1}", content=content)
        self.records[record.id] = record
        self.calls.append({"content": content, **kwargs})
        return record

    def delete(self, record_id):
        return self.records.pop(record_id, None) is not None


def _history(count: int = 60) -> ParsedHistory:
    turns = [
        Turn(
            Role.USER if index % 2 == 0 else Role.ASSISTANT,
            f"turn-{index}",
            ts=f"2025-01-01T00:00:{index % 60:02d}+00:00",
            seq=index,
        )
        for index in range(count)
    ]
    return ParsedHistory("conversations.txt", conversations=[Conversation("c1", turns=turns)])


def _preview(memory_count: int = 7) -> dict:
    memory = [
        {
            "id": f"m-{index}",
            "kind": "memory",
            "category": "user_fact",
            "content": f"fact-{index}",
            "disposition": "auto_approve",
            "review": "pending",
            "flags": [],
        }
        for index in range(memory_count)
    ]
    bond = []
    for index in range(6):
        bond.extend(
            [
                {
                    "id": f"bt-{index}",
                    "kind": "bond",
                    "signal_type": "turn_completed",
                    "disposition": "auto_approve",
                    "review": "pending",
                    "evidence": [{"turn_seq": index * 2}],
                },
                {
                    "id": f"bk-{index}",
                    "kind": "bond",
                    "signal_type": "thanked",
                    "disposition": "auto_approve",
                    "review": "pending",
                    "evidence": [{"turn_seq": index * 2 + 1}],
                },
            ]
        )
    return {"groups": {"memory": memory, "bond": bond, "style": []}}


def _smoke(tmp_path: Path):
    memory = FakeMemoryService()
    conversation = ConversationStore(tmp_path / "conversation.json", max_messages=40)
    bond = BondStateEngine(tmp_path / "bond.json")
    smoke = MigrationSmokeCommit(
        memory,
        conversation,
        bond,
        audit_dir=tmp_path / "audit",
        policy=SmokePolicy(
            memory_limit=5,
            conversation_turns=40,
            turn_completed_limit=2,
            thanked_limit=2,
        ),
        character_yaml_paths=[],
    )
    return smoke, memory, conversation, bond


def test_dry_run_is_bounded_and_has_no_writes(tmp_path: Path) -> None:
    smoke, memory, conversation, bond = _smoke(tmp_path)

    report = smoke.dry_run(_history(), _preview())

    assert report["memory"]["record_count"] == 5
    assert report["conversation"]["turn_count"] == 40
    assert len(report["bond"]["signals"]) == 4
    assert report["bond"]["predicted_state_after"]["trust_level"] == 0.04
    assert report["bond"]["predicted_state_after"]["familiarity_level"] == 0.01
    assert memory.records == {}
    assert conversation.load_working_window() == []
    assert bond.read().trust_level == 0.0
    assert not (tmp_path / "audit").exists()


def test_commit_uses_service_writes_and_creates_audit(tmp_path: Path) -> None:
    smoke, memory, conversation, bond = _smoke(tmp_path)

    audit = smoke.commit(_history(), _preview())

    assert audit["status"] == "committed"
    assert audit["run_id"].startswith("smoke-")
    assert len(memory.records) == 5
    assert all(call["trigger"] == f"migration:{audit['run_id']}" for call in memory.calls)
    assert conversation.window_size() == 40
    assert conversation.load_working_window()[0].content == "turn-20"
    assert bond.read().trust_level == 0.04
    audit_path = tmp_path / "audit" / f"{audit['run_id']}.json"
    assert json.loads(audit_path.read_text(encoding="utf-8"))["status"] == "committed"


def test_rollback_restores_all_three_stores(tmp_path: Path) -> None:
    smoke, memory, conversation, bond = _smoke(tmp_path)
    conversation.append_turn("user", "before", timestamp_ms=1)
    conversation_before = conversation.path.read_bytes()
    audit = smoke.commit(_history(), _preview())

    rolled_back = smoke.rollback(audit["run_id"])

    assert rolled_back["status"] == "rolled_back"
    assert memory.records == {}
    assert conversation.path.read_bytes() == conversation_before
    assert not bond.path.exists()


def test_manual_acceptance_and_unsafe_categories(tmp_path: Path) -> None:
    smoke, _, _, _ = _smoke(tmp_path)
    preview = _preview(memory_count=0)
    preview["groups"]["memory"] = [
        {
            "id": "safe",
            "kind": "memory",
            "category": "project",
            "content": "Firefly",
            "disposition": "needs_review",
            "review": "pending",
            "flags": ["roleplay"],
        },
        {
            "id": "relationship",
            "kind": "memory",
            "category": "relationship",
            "content": "unsafe",
            "disposition": "auto_approve",
            "review": "pending",
            "flags": [],
        },
    ]

    report = smoke.dry_run(_history(), preview, accepted_memory_ids=["safe"])

    assert report["memory"]["candidate_ids"] == ["safe"]


def test_question_fragment_is_not_committed_as_memory(tmp_path: Path) -> None:
    smoke, _, _, _ = _smoke(tmp_path)
    preview = _preview(memory_count=0)
    preview["groups"]["memory"] = [
        {
            "id": "bad-question",
            "kind": "memory",
            "category": "user_fact",
            "content": "什么大学的",
            "disposition": "auto_approve",
            "review": "pending",
            "flags": [],
        }
    ]

    report = smoke.dry_run(_history(), preview)

    assert report["memory"]["record_count"] == 0


def test_ceiling_blocks_additional_bond_growth(tmp_path: Path) -> None:
    smoke, _, _, bond = _smoke(tmp_path)
    for _ in range(35):
        bond.apply(BondSignal(BondSignalType.THANKED))

    report = smoke.dry_run(_history(), _preview())

    assert report["bond"]["predicted_state_after"]["trust_level"] == 0.7
    assert all(signal["type"] != "thanked" for signal in report["bond"]["signals"])


def test_chat_runner_preloads_smoke_conversation_history(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversation.json")
    store.append_exchange("以前的问题", "以前的回答", timestamp_ms=1)
    runtime = SimpleNamespace(conversation_store=store)

    runner = CharacterConversationRunner(runtime=runtime)

    assert runner.has_history is True
    assert runner.history == [
        {"role": "user", "content": "以前的问题"},
        {"role": "assistant", "content": "以前的回答"},
    ]
