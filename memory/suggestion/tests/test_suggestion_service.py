"""Tests for the suggestion service."""

from __future__ import annotations

from memory.suggestion.suggestion_service import SuggestionService


class FakeMemoryService:
    def __init__(self, fail: bool = False) -> None:
        self.remembered: list[dict] = []
        self.fail = fail

    def remember(
        self,
        user_input,
        *,
        category=None,
        trigger="explicit-command",
        permission=None,
        asserted_explicit=False,
    ):
        if self.fail:
            raise RuntimeError("write failed")
        record = {"content": user_input, "category": category, "trigger": trigger}
        self.remembered.append(record)
        return record


def test_detect_queues_pending() -> None:
    service = SuggestionService(FakeMemoryService())

    suggestions = service.detect("我毕业了")

    assert len(suggestions) == 1
    assert len(service.list_pending()) == 1


def test_accept_writes_through_memory_service() -> None:
    memory = FakeMemoryService()
    service = SuggestionService(memory)

    suggestion = service.detect("我喜欢猫")[0]
    record = service.accept(suggestion)

    assert record is not None
    assert memory.remembered[0]["content"] == "猫"
    assert memory.remembered[0]["category"] == "preference"
    assert memory.remembered[0]["trigger"] == "suggestion:stable_preference"
    assert service.list_pending() == []


def test_reject_discards() -> None:
    service = SuggestionService(FakeMemoryService())

    suggestion = service.detect("我要准备考研")[0]
    service.reject(suggestion)

    assert service.list_pending() == []


def test_accept_failure_keeps_pending() -> None:
    memory = FakeMemoryService(fail=True)
    service = SuggestionService(memory)

    suggestion = service.detect("我毕业了")[0]
    record = service.accept(suggestion)

    assert record is None
    assert service.list_pending() == [suggestion]  # still pending on failure
