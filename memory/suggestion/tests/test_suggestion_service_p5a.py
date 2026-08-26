"""Tests for SuggestionService P5A-1 upgrades: privacy, dedup, explicit remember."""

from __future__ import annotations

from typing import Any

from memory.records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy
from memory.suggestion.memory_candidate_detector import MemorySuggestion
from memory.suggestion.suggestion_service import SuggestionService


class FakeMemoryService:
    """Minimal fake that supports remember() and list()."""

    def __init__(self, existing_records: list[MemoryRecord] | None = None) -> None:
        self.remembered: list[dict] = []
        self._existing: list[MemoryRecord] = list(existing_records or [])

    def remember(
        self,
        user_input: str,
        *,
        category: str | None = None,
        trigger: str = "explicit-command",
        permission: str | None = None,
        asserted_explicit: bool = False,
    ) -> dict | None:
        record = {
            "content": user_input,
            "category": category,
            "trigger": trigger,
        }
        self.remembered.append(record)
        return record

    def list(
        self,
        *,
        category: str | None = None,
        source: str | None = None,
        before_ts: int | None = None,
    ) -> list[MemoryRecord]:
        if category:
            return [
                r for r in self._existing
                if r.category.value == category
            ]
        return list(self._existing)


class FakeExtractor:
    """Fake LLM extractor that returns pre-configured candidates."""

    def __init__(
        self,
        candidates: list[MemorySuggestion] | None = None,
        status: str = "success",
    ) -> None:
        self.candidates = candidates or []
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def extract(
        self,
        user_message: str,
        assistant_reply: str,
        context: Any = None,
    ) -> Any:
        from memory.suggestion.candidate_extractor import ExtractionResult
        self.calls.append((user_message, assistant_reply))
        return ExtractionResult(
            candidates=list(self.candidates),
            status=self.status,
        )


def _make_suggestion(
    content: str = "test",
    category: MemoryCategory = MemoryCategory.PREFERENCE,
    reason: str = "test",
    confidence: float = 0.5,
) -> MemorySuggestion:
    return MemorySuggestion(
        content=content,
        category=category,
        reason=reason,
        evidence=(content,),
        confidence=confidence,
    )


# =====================================================================
# Extract candidates pipeline
# =====================================================================


class TestExtractCandidates:
    """Tests for SuggestionService.extract_candidates()."""

    def test_disabled_auto_extract_returns_zero(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(
            memory,
            auto_extract_enabled=False,
        )
        result = service.extract_candidates("test", "reply")
        assert result.status == "zero_candidates"
        assert result.candidates == []

    def test_no_extractor_returns_unavailable(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
        )
        result = service.extract_candidates("test", "reply")
        assert result.status == "provider_unavailable"

    def test_extractor_success_queues_candidates(self) -> None:
        memory = FakeMemoryService()
        extractor = FakeExtractor(
            candidates=[_make_suggestion("我喜欢猫")],
        )
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        result = service.extract_candidates("我喜欢猫", "好的")
        assert result.status == "success"
        assert len(service.list_pending()) == 1
        assert service.list_pending()[0].content == "我喜欢猫"

    def test_extractor_zero_candidates(self) -> None:
        memory = FakeMemoryService()
        extractor = FakeExtractor(candidates=[], status="zero_candidates")
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        result = service.extract_candidates("你好", "你好！")
        assert result.status == "zero_candidates"
        assert service.list_pending() == []

    def test_extractor_failure_is_safe_noop(self) -> None:
        memory = FakeMemoryService()
        extractor = FakeExtractor(status="parse_failure")
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        result = service.extract_candidates("test", "reply")
        assert result.status == "parse_failure"
        assert service.list_pending() == []


# =====================================================================
# Privacy filtering
# =====================================================================


class TestPrivacyFiltering:
    """Tests that privacy violations are rejected before becoming pending."""

    def test_api_key_rejected(self) -> None:
        memory = FakeMemoryService()
        suggestion = _make_suggestion("sk-abc123def456ghi789jkl012mno345")
        extractor = FakeExtractor(candidates=[suggestion])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        service.extract_candidates("test", "reply")
        assert service.list_pending() == []

    def test_password_rejected(self) -> None:
        memory = FakeMemoryService()
        suggestion = _make_suggestion("password=mypassword123")
        extractor = FakeExtractor(candidates=[suggestion])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        service.extract_candidates("test", "reply")
        assert service.list_pending() == []

    def test_jwt_rejected(self) -> None:
        memory = FakeMemoryService()
        # Valid JWT-like token (3 base64url segments separated by dots)
        suggestion = _make_suggestion(
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        extractor = FakeExtractor(candidates=[suggestion])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        service.extract_candidates("test", "reply")
        assert service.list_pending() == []

    def test_bearer_token_rejected(self) -> None:
        memory = FakeMemoryService()
        suggestion = _make_suggestion("Bearer abcdefghijklmnopqrstuvwxyz")
        extractor = FakeExtractor(candidates=[suggestion])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        service.extract_candidates("test", "reply")
        assert service.list_pending() == []

    def test_clean_candidate_passes_privacy(self) -> None:
        memory = FakeMemoryService()
        suggestion = _make_suggestion("我喜欢喝咖啡")
        extractor = FakeExtractor(candidates=[suggestion])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        service.extract_candidates("我喜欢喝咖啡", "好的")
        assert len(service.list_pending()) == 1


# =====================================================================
# Deduplication
# =====================================================================


class TestDeduplication:
    """Tests that duplicate suggestions are suppressed."""

    def test_duplicate_pending_suggestion_suppressed(self) -> None:
        memory = FakeMemoryService()
        # First round creates a pending suggestion
        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor1,
        )
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1

        # Second round with same content should be suppressed
        extractor2 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service.extractor = extractor2
        result = service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1  # still just one

    def test_duplicate_existing_memory_record_suppressed(self) -> None:
        existing = [
            MemoryRecord.create(
                category=MemoryCategory.PREFERENCE,
                content="我喜欢喝咖啡",
                trigger="test",
            )
        ]
        memory = FakeMemoryService(existing_records=existing)
        extractor = FakeExtractor(candidates=[_make_suggestion("我喜欢喝咖啡")])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        service.extract_candidates("我喜欢喝咖啡", "好的")
        assert service.list_pending() == []

    def test_same_text_different_normalization_suppressed(self) -> None:
        """'我喜欢猫' and '我 喜欢 猫' should be treated as duplicates."""
        memory = FakeMemoryService()
        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor1,
        )
        service.extract_candidates("我喜欢猫", "好的")

        # Different spacing but same normalized content
        extractor2 = FakeExtractor(candidates=[_make_suggestion("我 喜欢 猫")])
        service.extractor = extractor2
        service.extract_candidates("我 喜欢 猫", "好的")
        assert len(service.list_pending()) == 1

    def test_different_content_not_suppressed(self) -> None:
        memory = FakeMemoryService()
        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor1,
        )
        service.extract_candidates("我喜欢猫", "好的")

        extractor2 = FakeExtractor(candidates=[_make_suggestion("我喜欢狗")])
        service.extractor = extractor2
        service.extract_candidates("我喜欢狗", "好的")
        assert len(service.list_pending()) == 2

    def test_duplicate_in_same_batch_suppressed(self) -> None:
        """Two identical candidates in the same extraction batch."""
        memory = FakeMemoryService()
        candidates = [
            _make_suggestion("我喜欢猫"),
            _make_suggestion("我喜欢猫"),
        ]
        extractor = FakeExtractor(candidates=candidates)
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1


# =====================================================================
# Explicit remember
# =====================================================================


class TestExplicitRemember:
    """Tests for SuggestionService.handle_explicit_remember()."""

    def test_explicit_remember_creates_pending(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        suggestions = service.handle_explicit_remember("请记住用户叫小明")
        assert len(suggestions) == 1
        assert suggestions[0].content == "用户叫小明"
        assert suggestions[0].reason == "explicit_remember"
        assert suggestions[0].confidence == 0.95
        assert len(service.list_pending()) == 1

    def test_explicit_remember_strips_prefix(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        suggestions = service.handle_explicit_remember("请帮我记住一下用户叫小红")
        assert len(suggestions) == 1
        assert suggestions[0].content == "用户叫小红"

    def test_explicit_remember_with_english(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        suggestions = service.handle_explicit_remember("Please remember user is Alice")
        assert len(suggestions) == 1
        assert suggestions[0].content == "user is Alice"

    def test_explicit_remember_api_key_rejected(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        # The red-line detector uses \b word boundary, so the key must be
        # preceded by a whitespace or start-of-string boundary.
        suggestions = service.handle_explicit_remember("请记住 sk-abcdefghijklmnop1234567890")
        assert suggestions == []

    def test_explicit_remember_duplicate_suppressed(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        service.handle_explicit_remember("请记住用户叫小明")
        suggestions = service.handle_explicit_remember("请记住用户叫小明")
        assert suggestions == []
        assert len(service.list_pending()) == 1

    def test_explicit_remember_with_custom_confidence(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        suggestions = service.handle_explicit_remember("请记住重要事项", confidence=1.0)
        assert len(suggestions) == 1
        assert suggestions[0].confidence == 1.0

    def test_empty_explicit_remember_returns_empty(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        assert service.handle_explicit_remember("") == []
        assert service.handle_explicit_remember("记住") == []  # no content after prefix


# =====================================================================
# Accept / Reject still work
# =====================================================================


class TestAcceptReject:
    """Verify that accept/reject still work correctly with P5A upgrades."""

    def test_accept_writes_through_memory_service(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        suggestion = _make_suggestion("我喜欢猫")
        service._pending.append(suggestion)
        record = service.accept(suggestion)
        assert record is not None
        assert memory.remembered[0]["content"] == "我喜欢猫"
        assert service.list_pending() == []

    def test_reject_discards(self) -> None:
        memory = FakeMemoryService()
        service = SuggestionService(memory)
        suggestion = _make_suggestion("我喜欢猫")
        service._pending.append(suggestion)
        service.reject(suggestion)
        assert service.list_pending() == []

    def test_accept_failure_keeps_pending(self) -> None:
        class FailingMemoryService:
            def remember(self, *a, **kw):
                raise RuntimeError("write failed")
            def list(self, **kw):
                return []

        memory = FailingMemoryService()
        service = SuggestionService(memory)
        suggestion = _make_suggestion("我喜欢猫")
        service._pending.append(suggestion)
        record = service.accept(suggestion)
        assert record is None
        assert service.list_pending() == [suggestion]


# =====================================================================
# Multiple candidates cap
# =====================================================================


class TestMaxCandidatesPerTurn:
    """Tests that max_candidates_per_turn is respected."""

    def test_max_candidates_enforced(self) -> None:
        memory = FakeMemoryService()
        candidates = [
            _make_suggestion(f"item{i}") for i in range(10)
        ]
        extractor = FakeExtractor(candidates=candidates)
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
            max_candidates_per_turn=3,
        )
        service.extract_candidates("test", "reply")
        # max_candidates_per_turn caps the pending suggestions added
        assert len(service._pending) <= 3
