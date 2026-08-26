"""Tests for the narrative profile builder."""

from __future__ import annotations

from pathlib import Path

from tools.firefly_memory_importer.models import Role
from tools.firefly_memory_importer.analyzer.candidates import (
    Band,
    Disposition,
    NarrativeCandidate,
    NarrativeType,
    SourceRef,
)
from tools.firefly_memory_importer.narrative.narrative_profile import (
    build_profile,
    load,
    to_candidates,
    write,
)


def _ref() -> SourceRef:
    return SourceRef("c1", 0, Role.USER, ts="2024-10-29T12:00:00Z", message_id="m-1")


def _candidate(narrative_type: NarrativeType, content: str) -> NarrativeCandidate:
    return NarrativeCandidate(
        id=f"n-{narrative_type.value}",
        narrative_type=narrative_type,
        content=content,
        confidence=0.4,
        band=Band.MEDIUM,
        evidence=(_ref(),),
        rule=f"narr.{narrative_type.value}",
        disposition=Disposition.NEEDS_REVIEW,
        flags=("sensitive",),
    )


def test_build_profile_groups_by_type() -> None:
    candidates = [
        _candidate(NarrativeType.LIFE_EVENT, "毕业了"),
        _candidate(NarrativeType.LONG_TERM_GOAL, "三年后开一家公司"),
        _candidate(NarrativeType.SHARED_EXPERIENCE, "一起度过了那个夏天"),
    ]

    profile = build_profile(
        candidates,
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )

    assert len(profile["profile"]["life_event"]) == 1
    assert len(profile["profile"]["long_term_goal"]) == 1
    assert len(profile["profile"]["shared_experience"]) == 1
    assert profile["profile"]["life_event"][0]["content"] == "毕业了"


def test_to_candidates_reconstructs() -> None:
    candidates = [_candidate(NarrativeType.LIFE_EVENT, "毕业了")]
    profile = build_profile(
        candidates,
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )

    reconstructed = to_candidates(profile)

    assert len(reconstructed) == 1
    assert reconstructed[0].narrative_type is NarrativeType.LIFE_EVENT
    assert reconstructed[0].content == "毕业了"


def test_evidence_chain_preserved() -> None:
    profile = build_profile(
        [_candidate(NarrativeType.LIFE_EVENT, "毕业了")],
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )

    entry = profile["profile"]["life_event"][0]
    assert entry["evidence"][0]["conversation_id"] == "c1"
    assert entry["evidence"][0]["message_id"] == "m-1"


def test_write_load_roundtrip(tmp_path: Path) -> None:
    profile = build_profile(
        [_candidate(NarrativeType.LIFE_EVENT, "毕业了")],
        run_id="run-1",
        source="doubao",
        source_file="conversations.txt",
        rule_version=1,
    )
    path = tmp_path / "narrative_profile.json"

    write(profile, path)
    loaded = load(path)

    assert loaded["run_id"] == "run-1"
    assert len(loaded["profile"]["life_event"]) == 1
    assert len(loaded["candidates"]) == 1
