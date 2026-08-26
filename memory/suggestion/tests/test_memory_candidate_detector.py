"""Tests for the memory candidate detector."""

from __future__ import annotations

from memory.records import MemoryCategory
from memory.suggestion.memory_candidate_detector import MemoryCandidateDetector


def test_detects_long_term_goal() -> None:
    result = MemoryCandidateDetector().detect("我要准备考研")

    assert len(result) == 1
    suggestion = result[0]
    assert suggestion.category is MemoryCategory.PROJECT
    assert suggestion.content == "准备考研"
    assert suggestion.reason == "long_term_goal"


def test_detects_stable_preference() -> None:
    result = MemoryCandidateDetector().detect("我喜欢喝咖啡")

    assert len(result) == 1
    suggestion = result[0]
    assert suggestion.category is MemoryCategory.PREFERENCE
    assert suggestion.content == "喝咖啡"
    assert suggestion.reason == "stable_preference"


def test_detects_important_event() -> None:
    result = MemoryCandidateDetector().detect("我毕业了")

    assert len(result) == 1
    suggestion = result[0]
    assert suggestion.category is MemoryCategory.SHARED_EXPERIENCE
    assert suggestion.content == "毕业了"
    assert suggestion.reason == "important_event"


def test_no_match_returns_empty() -> None:
    assert MemoryCandidateDetector().detect("你好呀") == []


def test_empty_message_returns_empty() -> None:
    assert MemoryCandidateDetector().detect("") == []
    assert MemoryCandidateDetector().detect("   ") == []


def test_suggestion_has_evidence_and_confidence() -> None:
    result = MemoryCandidateDetector().detect("我搬家了")

    suggestion = result[0]
    assert suggestion.evidence == ("我搬家了",)
    assert suggestion.confidence == 0.8
    assert suggestion.to_dict()["category"] == "shared_experience"
