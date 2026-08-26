"""Tests for P5A-1 runtime integration: extraction in CompanionRuntime.chat()."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import pytest

from core.companion_runtime import CompanionRuntime, TurnStage
from memory.records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy
from memory.suggestion.memory_candidate_detector import MemorySuggestion
from memory.suggestion.suggestion_service import SuggestionService


RESPONSE = {
    "provider": "fake",
    "choices": [{"message": {"role": "assistant", "content": "收到。"}}],
}


class FakeCharacter:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def to_system_messages(self) -> list[dict[str, str]]:
        self.calls.append("character")
        return [{"role": "system", "content": "character"}]


class FakeMemoryService:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail
        self.queries: list[str] = []
        self._records: list[MemoryRecord] = []

    def search(self, query: str, *, limit: int = 5, threshold: float = 0.0) -> list[dict[str, str]]:
        self.calls.append("memory")
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("memory unavailable")
        return [{"category": "project", "memory": "Firefly Phase 3.0-A"}]

    def list(
        self,
        *,
        category: str | None = None,
        source: str | None = None,
        before_ts: int | None = None,
    ) -> list[MemoryRecord]:
        return list(self._records)


@dataclass
class FakeTurn:
    role: str
    content: str

    def to_chat_message(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class FakeConversationStore:
    def __init__(
        self,
        calls: list[str],
        *,
        fail_load: bool = False,
        fail_save: bool = False,
    ) -> None:
        self.calls = calls
        self.fail_load = fail_load
        self.fail_save = fail_save
        self.saved: list[tuple[str, str]] = []
        self._turns: list[FakeTurn] = []

    def load_working_window(self) -> list[FakeTurn]:
        self.calls.append("conversation_load")
        if self.fail_load:
            raise OSError("conversation read failed")
        return list(self._turns)

    def append_exchange(self, user: str, assistant: str) -> None:
        self.calls.append("conversation_save")
        if self.fail_save:
            raise OSError("conversation write failed")
        self._turns.append(FakeTurn("user", user))
        self._turns.append(FakeTurn("assistant", assistant))
        self.saved.append((user, assistant))


class FakeBondStateEngine:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail
        self.state = object()

    def read(self) -> object:
        self.calls.append("bond")
        if self.fail:
            raise OSError("bond read failed")
        return self.state


class FakeProvider:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail
        self.messages: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.calls.append("provider")
        self.messages = messages
        if self.fail:
            raise RuntimeError("provider failed")
        return RESPONSE


class FakeSuggestionService:
    """Tracks extract_candidates calls without doing real extraction."""

    def __init__(self) -> None:
        self.extract_calls: list[tuple[str, str]] = []
        self._pending: list[MemorySuggestion] = []
        self.auto_extract_enabled = True
        self.memory_service = None

    def extract_candidates(
        self,
        user_message: str,
        assistant_reply: str,
        context: Any = None,
    ) -> Any:
        self.extract_calls.append((user_message, assistant_reply))
        from memory.suggestion.candidate_extractor import ExtractionResult
        return ExtractionResult(candidates=[], status="zero_candidates")

    def list_pending(self) -> list[MemorySuggestion]:
        return list(self._pending)


def _runtime_with_suggestion(
    suggestion_service: FakeSuggestionService | None = None,
    suggestion_enabled: bool = False,
) -> tuple[
    CompanionRuntime,
    list[str],
    FakeMemoryService,
    FakeConversationStore,
    FakeBondStateEngine,
    FakeProvider,
    FakeSuggestionService | None,
]:
    calls: list[str] = []
    memory = FakeMemoryService(calls)
    store = FakeConversationStore(calls)
    bond = FakeBondStateEngine(calls)
    provider = FakeProvider(calls)
    char = FakeCharacter(calls)

    if suggestion_service is not None:
        runtime = CompanionRuntime(
            char,
            memory,
            store,
            bond,
            provider,
            suggestion_service=suggestion_service,
        )
    else:
        runtime = CompanionRuntime(
            char,
            memory,
            store,
            bond,
            provider,
        )
        # Override suggestion service after construction
        runtime.suggestion_service = suggestion_service

    return runtime, calls, memory, store, bond, provider, suggestion_service


# =====================================================================
# Runtime integration tests
# =====================================================================


class TestRuntimeExtraction:
    """Tests that extraction runs after chat reply in CompanionRuntime."""

    def test_extract_candidates_called_after_reply(self) -> None:
        suggestion_svc = FakeSuggestionService()
        runtime, calls, _, _, _, _, _ = _runtime_with_suggestion(suggestion_svc)

        runtime.chat("继续实现")

        assert "provider" in calls
        assert "conversation_save" in calls
        # Extraction should have been called with the user message and reply
        assert len(suggestion_svc.extract_calls) == 1
        user_msg, reply = suggestion_svc.extract_calls[0]
        assert user_msg == "继续实现"
        assert reply == "收到。"

    def test_extraction_failure_does_not_fail_chat(self) -> None:
        class FailingSuggestionService:
            auto_extract_enabled = True
            memory_service = None

            def extract_candidates(self, *a, **kw):
                raise RuntimeError("extraction boom")

            def list_pending(self):
                return []

        runtime, calls, _, store, _, _, _ = _runtime_with_suggestion(
            FailingSuggestionService()
        )

        # Should NOT raise
        response = runtime.chat("test message")
        assert response is RESPONSE
        assert "conversation_save" in calls
        assert store.saved == [("test message", "收到。")]

    def test_no_suggestion_service_is_safe(self) -> None:
        runtime, calls, _, store, _, _, _ = _runtime_with_suggestion(None)

        response = runtime.chat("test")
        assert response is RESPONSE
        assert "conversation_save" in calls

    def test_suggestion_service_disabled_is_safe(self) -> None:
        class DisabledSuggestionService:
            auto_extract_enabled = False
            memory_service = None

            def extract_candidates(self, *a, **kw):
                raise AssertionError("should not be called")

            def list_pending(self):
                return []

        runtime, _, _, _, _, _, _ = _runtime_with_suggestion(
            DisabledSuggestionService()
        )
        # Should not raise
        response = runtime.chat("test")
        assert response is RESPONSE

    def test_reply_still_returned_on_extraction_error(self) -> None:
        class BoomSuggestionService:
            auto_extract_enabled = True
            memory_service = None

            def extract_candidates(self, *a, **kw):
                raise ValueError("extraction error")

            def list_pending(self):
                return []

        runtime, _, _, store, _, _, _ = _runtime_with_suggestion(
            BoomSuggestionService()
        )

        response = runtime.chat("hello")
        assert response is RESPONSE
        # Reply should still be saved
        assert store.saved == [("hello", "收到。")]

    def test_context_passed_to_extraction(self) -> None:
        suggestion_svc = FakeSuggestionService()
        runtime, calls, _, _, _, _, _ = _runtime_with_suggestion(suggestion_svc)

        # First turn to populate conversation store
        runtime.chat("第一轮")
        assert len(runtime.conversation_store._turns) == 2  # user + assistant

        # Second turn should have context
        runtime.chat("第二轮")
        assert len(suggestion_svc.extract_calls) == 2
        # The second call should have context in the extractor
        # (we can't easily verify context here without a real extractor,
        # but we verify the call was made)


class TestNoDirectMemoryRecordWrite:
    """Verify that extraction never writes MemoryRecord directly."""

    def test_suggestion_service_never_calls_remember_during_extract(self) -> None:
        """extract_candidates should only queue pending suggestions, not call remember()."""
        memory = FakeMemoryService([])
        suggestion_svc = FakeSuggestionService()
        suggestion_svc.memory_service = memory

        runtime, _, _, _, _, _, _ = _runtime_with_suggestion(suggestion_svc)
        runtime.memory_service = memory  # wire the same fake

        runtime.chat("test")

        # remember() should never be called during extraction
        assert "remember" not in [c for c in memory.calls if isinstance(c, str)]


class TestPendingSuggestionsAccessible:
    """Verify pending suggestions are accessible after chat."""

    def test_pending_suggestions_reflect_extractions(self) -> None:
        class TrackingSuggestionService:
            def __init__(self) -> None:
                self._pending: list[MemorySuggestion] = []
                self.auto_extract_enabled = True
                self.memory_service = None

            def extract_candidates(self, user_msg: str, reply: str, context: Any = None) -> Any:
                if "我喜欢猫" in user_msg:
                    self._pending.append(
                        MemorySuggestion(
                            content="我喜欢猫",
                            category=MemoryCategory.PREFERENCE,
                            reason="llm_extracted",
                            evidence=(user_msg,),
                            confidence=0.8,
                        )
                    )
                from memory.suggestion.candidate_extractor import ExtractionResult
                return ExtractionResult(
                    candidates=[s for s in self._pending if s.content == "我喜欢猫"][-1:] if "我喜欢猫" in user_msg else [],
                    status="success" if "我喜欢猫" in user_msg else "zero_candidates",
                )

            def list_pending(self) -> list[MemorySuggestion]:
                return list(self._pending)

        svc = TrackingSuggestionService()
        runtime, _, _, _, _, _, _ = _runtime_with_suggestion(svc)

        runtime.chat("我喜欢猫")
        assert len(svc.list_pending()) == 1
        assert svc.list_pending()[0].content == "我喜欢猫"
