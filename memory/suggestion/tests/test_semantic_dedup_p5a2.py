"""Tests for semantic suggestion dedup (P5A-2).

Covers:
- Exact dedup (regression)
- Normalized exact dedup (regression)
- Semantic paraphrase suppression (preference, user_fact, project)
- Genuine different facts NOT suppressed
- Same text different category NOT merged
- Existing MemoryRecord semantic duplicate suppression
- Pending suggestion semantic duplicate suppression
- Failure degradation (embedding unavailable → exact fallback)
- Privacy guard still runs correctly
- Regression: P4 MemoryService dedup unchanged
- Regression: P5A extraction unchanged
- Config defaults
"""

from __future__ import annotations

from typing import Any

import pytest

from memory.records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy
from memory.suggestion.candidate_extractor import ExtractionResult, MemoryCandidateExtractor
from memory.suggestion.memory_candidate_detector import MemorySuggestion
from memory.suggestion.semantic_dedup import SemanticDedup, _SemanticMatch
from memory.suggestion.suggestion_service import SuggestionService


# =====================================================================
# Helpers
# =====================================================================


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


def _make_record(
    content: str,
    category: MemoryCategory = MemoryCategory.PREFERENCE,
    trigger: str = "test",
) -> MemoryRecord:
    return MemoryRecord.create(
        category=category,
        content=content,
        trigger=trigger,
        source=MemorySource.EXPLICIT,
    )


# =====================================================================
# SemanticDedup unit tests
# =====================================================================


class TestSemanticDedup:
    """Tests for the SemanticDedup class directly."""

    def test_threshold_config(self) -> None:
        dedup = SemanticDedup(threshold=0.90)
        assert dedup.threshold == 0.90

    def test_empty_pending_returns_no_duplicate(self) -> None:
        dedup = SemanticDedup()
        candidate = _make_suggestion("我喜欢猫")
        result = dedup.check_pending(candidate, [])
        assert result is not None
        assert result.is_duplicate is False

    def test_empty_records_returns_no_duplicate(self) -> None:
        dedup = SemanticDedup()
        candidate = _make_suggestion("我喜欢猫")
        result = dedup.check_existing_records(candidate, [])
        assert result is not None
        assert result.is_duplicate is False

    def test_different_category_skipped_in_pending(self) -> None:
        """Semantic dedup only compares within same category."""
        dedup = SemanticDedup(threshold=0.5)
        candidate = _make_suggestion("我喜欢猫", category=MemoryCategory.PREFERENCE)
        existing = [_make_suggestion("我住在杭州", category=MemoryCategory.USER_FACT)]
        result = dedup.check_pending(candidate, existing)
        assert result is not None
        assert result.is_duplicate is False

    def test_different_category_skipped_in_records(self) -> None:
        """Semantic dedup only compares within same category."""
        dedup = SemanticDedup(threshold=0.5)
        candidate = _make_suggestion("我喜欢猫", category=MemoryCategory.PREFERENCE)
        existing = [_make_record("我住在杭州", category=MemoryCategory.USER_FACT)]
        result = dedup.check_existing_records(candidate, existing)
        assert result is not None
        assert result.is_duplicate is False


# =====================================================================
# SuggestionService: exact dedup regression
# =====================================================================


class TestExactDedupRegression:
    """Ensure existing exact dedup still works after semantic layer addition."""

    def test_exact_duplicate_suppressed(self) -> None:
        memory = FakeMemoryService()
        extractor = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
            semantic_dedup_enabled=False,  # disable semantic to test exact only
        )
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1

        extractor2 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service.extractor = extractor2
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1  # still just one

    def test_normalized_duplicate_suppressed(self) -> None:
        """'我喜欢猫' and '我 喜欢 猫' should be treated as duplicates."""
        memory = FakeMemoryService()
        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor1,
            semantic_dedup_enabled=False,
        )
        service.extract_candidates("我喜欢猫", "好的")

        extractor2 = FakeExtractor(candidates=[_make_suggestion("我 喜欢 猫")])
        service.extractor = extractor2
        service.extract_candidates("我 喜欢 猫", "好的")
        assert len(service.list_pending()) == 1


def _make_semantic_service(
    semantic_dedup_enabled: bool = True,
    threshold: float = 0.85,
) -> SuggestionService:
    """Create a SuggestionService with a working SemanticDedup.

    We replace the real _get_embedding with a fake that maps known
    paraphrase pairs to identical vectors, so we can test the dedup
    logic without needing the actual FastEmbed model.
    """
    memory = FakeMemoryService()
    service = SuggestionService(
        memory,
        auto_extract_enabled=True,
        extractor=None,
        semantic_dedup_enabled=semantic_dedup_enabled,
        semantic_dedup_threshold=threshold,
    )
    return service


# =====================================================================
# SuggestionService: semantic dedup — paraphrase suppression
# =====================================================================


class TestSemanticDedupParaphrase:
    """Tests that paraphrased same-preference candidates are suppressed."""

    def test_paraphrase_preference_suppressed(self) -> None:
        """'我喜欢机械键盘' and '我比较喜欢用机械键盘' should be suppressed."""
        service = _make_semantic_service()

        # First candidate goes through
        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢机械键盘")])
        service.extractor = extractor1
        service.extract_candidates("我喜欢机械键盘", "好的")
        assert len(service.list_pending()) == 1

        # Now inject a semantic-duplicate paraphrase by patching _get_embedding
        # to return identical vectors for these two texts
        original_get = service.semantic_dedup._get_embedding if service.semantic_dedup else None

        def fake_get(text: str) -> list[float] | None:
            # Map paraphrase pairs to same vector
            paraphrase_map = {
                "我喜欢机械键盘": [1.0, 0.0],
                "我比较喜欢用机械键盘": [1.0, 0.0],  # same vector = duplicate
                "我喜欢狗": [0.0, 1.0],  # different vector = not duplicate
                "我住在杭州": [0.1, 0.2],
                "我的常住城市是杭州": [0.1, 0.2],  # same vector = duplicate
            }
            return paraphrase_map.get(text, None)

        if original_get:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        extractor2 = FakeExtractor(candidates=[_make_suggestion("我比较喜欢用机械键盘")])
        service.extractor = extractor2
        result = service.extract_candidates("我比较喜欢用机械键盘", "好的")
        assert len(service.list_pending()) == 1  # still just one
        assert result.status == "success"
        assert result.candidates == []  # suppressed

    def test_paraphrase_user_fact_suppressed(self) -> None:
        """'我住在杭州' and '我的常住城市是杭州' should be suppressed."""
        service = _make_semantic_service()

        extractor1 = FakeExtractor(candidates=[_make_suggestion("我住在杭州", MemoryCategory.USER_FACT)])
        service.extractor = extractor1
        service.extract_candidates("我住在杭州", "好的")
        assert len(service.list_pending()) == 1

        def fake_get(text: str) -> list[float] | None:
            paraphrase_map = {
                "我住在杭州": [0.1, 0.2],
                "我的常住城市是杭州": [0.1, 0.2],
            }
            return paraphrase_map.get(text, None)

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        extractor2 = FakeExtractor(candidates=[_make_suggestion("我的常住城市是杭州", MemoryCategory.USER_FACT)])
        service.extractor = extractor2
        service.extract_candidates("我的常住城市是杭州", "好的")
        assert len(service.list_pending()) == 1  # still just one

    def test_paraphrase_project_suppressed(self) -> None:
        """'以后写 Python 测试优先 pytest' and '我更倾向用 pytest 做 Python 测试' should be suppressed."""
        service = _make_semantic_service()

        extractor1 = FakeExtractor(candidates=[_make_suggestion("以后写 Python 测试优先 pytest", MemoryCategory.PROJECT)])
        service.extractor = extractor1
        service.extract_candidates("以后写 Python 测试优先 pytest", "好的")
        assert len(service.list_pending()) == 1

        def fake_get(text: str) -> list[float] | None:
            paraphrase_map = {
                "以后写 Python 测试优先 pytest": [0.3, 0.4],
                "我更倾向用 pytest 做 Python 测试": [0.3, 0.4],
            }
            return paraphrase_map.get(text, None)

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        extractor2 = FakeExtractor(candidates=[_make_suggestion("我更倾向用 pytest 做 Python 测试", MemoryCategory.PROJECT)])
        service.extractor = extractor2
        service.extract_candidates("我更倾向用 pytest 做 Python 测试", "好的")
        assert len(service.list_pending()) == 1  # still just one


# =====================================================================
# SuggestionService: semantic dedup — genuine different facts NOT suppressed
# =====================================================================


class TestSemanticDedupDifferentFacts:
    """Ensure genuinely different facts are NOT suppressed."""

    def test_different_preferences_not_suppressed(self) -> None:
        """'我喜欢机械键盘' and '我喜欢薄膜键盘' should NOT be merged."""
        service = _make_semantic_service()

        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢机械键盘")])
        service.extractor = extractor1
        service.extract_candidates("我喜欢机械键盘", "好的")
        assert len(service.list_pending()) == 1

        def fake_get(text: str) -> list[float] | None:
            paraphrase_map = {
                "我喜欢机械键盘": [1.0, 0.0],
                "我喜欢薄膜键盘": [0.0, 1.0],  # different vector
            }
            return paraphrase_map.get(text, None)

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        extractor2 = FakeExtractor(candidates=[_make_suggestion("我喜欢薄膜键盘")])
        service.extractor = extractor2
        service.extract_candidates("我喜欢薄膜键盘", "好的")
        assert len(service.list_pending()) == 2  # both kept


# =====================================================================
# SuggestionService: semantic dedup — same text different category
# =====================================================================


class TestSemanticDedupDifferentCategory:
    """Same text in different category should NOT be merged."""

    def test_same_text_different_category_not_merged(self) -> None:
        """Same semantic content in different category should both be kept.

        We use '我喜欢猫' (preference) and '我特别喜欢猫' (user_fact) —
        different text (so Layer 1 exact dedup passes) but same vector
        (so Layer 2 semantic dedup would flag it IF it didn't respect
        category boundaries).
        """
        service = _make_semantic_service()

        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫", MemoryCategory.PREFERENCE)])
        service.extractor = extractor1
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1

        def fake_get(text: str) -> list[float] | None:
            # Both texts map to same vector → semantic duplicate
            return [0.5, 0.5]

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        # Different text, different category → should NOT be suppressed
        extractor2 = FakeExtractor(candidates=[_make_suggestion("我特别喜欢猫", MemoryCategory.USER_FACT)])
        service.extractor = extractor2
        service.extract_candidates("我特别喜欢猫", "好的")
        assert len(service.list_pending()) == 2  # both kept (different category)


# =====================================================================
# SuggestionService: semantic dedup — existing MemoryRecord
# =====================================================================


class TestSemanticDedupExistingRecord:
    """Semantic dedup against saved MemoryRecords."""

    def test_semantic_duplicate_of_saved_record_suppressed(self) -> None:
        """Paraphrase of a saved MemoryRecord should be suppressed."""
        existing = [
            _make_record("我喜欢机械键盘", MemoryCategory.PREFERENCE)
        ]
        memory = FakeMemoryService(existing_records=existing)
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
            semantic_dedup_enabled=True,
        )

        def fake_get(text: str) -> list[float] | None:
            paraphrase_map = {
                "我喜欢机械键盘": [1.0, 0.0],
                "我比较喜欢用机械键盘": [1.0, 0.0],
            }
            return paraphrase_map.get(text, None)

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        extractor = FakeExtractor(candidates=[_make_suggestion("我比较喜欢用机械键盘")])
        service.extractor = extractor
        result = service.extract_candidates("我比较喜欢用机械键盘", "好的")
        assert len(service.list_pending()) == 0  # suppressed
        assert result.status == "success"
        assert result.candidates == []

    def test_different_text_of_saved_record_not_suppressed(self) -> None:
        """Different text should NOT be suppressed even if category matches."""
        existing = [
            _make_record("我喜欢机械键盘", MemoryCategory.PREFERENCE)
        ]
        memory = FakeMemoryService(existing_records=existing)
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
            semantic_dedup_enabled=True,
        )

        def fake_get(text: str) -> list[float] | None:
            paraphrase_map = {
                "我喜欢机械键盘": [1.0, 0.0],
                "我喜欢薄膜键盘": [0.0, 1.0],  # different vector
            }
            return paraphrase_map.get(text, None)

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        extractor = FakeExtractor(candidates=[_make_suggestion("我喜欢薄膜键盘")])
        service.extractor = extractor
        service.extract_candidates("我喜欢薄膜键盘", "好的")
        assert len(service.list_pending()) == 1  # kept


# =====================================================================
# SuggestionService: semantic dedup — pending suggestion
# =====================================================================


class TestSemanticDedupPending:
    """Semantic dedup against existing pending suggestions."""

    def test_semantic_duplicate_of_pending_suppressed(self) -> None:
        """Paraphrase of an existing pending suggestion should be suppressed."""
        memory = FakeMemoryService()
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
            semantic_dedup_enabled=True,
        )

        # First round creates a pending suggestion
        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service.extractor = extractor1
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1

        def fake_get(text: str) -> list[float] | None:
            paraphrase_map = {
                "我喜欢猫": [0.5, 0.5],
                "我特别喜欢猫": [0.5, 0.5],  # same vector = duplicate
            }
            return paraphrase_map.get(text, None)

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        extractor2 = FakeExtractor(candidates=[_make_suggestion("我特别喜欢猫")])
        service.extractor = extractor2
        result = service.extract_candidates("我特别喜欢猫", "好的")
        assert len(service.list_pending()) == 1  # still just one
        assert result.candidates == []  # suppressed


# =====================================================================
# SuggestionService: failure degradation
# =====================================================================


class TestFailureDegradation:
    """Ensure semantic dedup failure falls back to exact dedup."""

    def test_embedding_unavailable_falls_back_to_exact(self) -> None:
        """When embedding fails, exact dedup should still work."""
        memory = FakeMemoryService()
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
            semantic_dedup_enabled=True,
        )

        # Make _get_embedding always return None (simulates embedding failure)
        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = lambda text: None  # type: ignore

        # First candidate goes through
        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service.extractor = extractor1
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1

        # Exact duplicate should still be suppressed (Layer 1 still works)
        extractor2 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service.extractor = extractor2
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1  # still just one

        # Different content should be added (semantic layer failed, exact is different)
        extractor3 = FakeExtractor(candidates=[_make_suggestion("我喜欢狗")])
        service.extractor = extractor3
        service.extract_candidates("我喜欢狗", "好的")
        assert len(service.list_pending()) == 2  # both kept

    def test_semantic_comparison_error_safe_fallback(self) -> None:
        """Semantic comparison errors should not crash the pipeline."""
        memory = FakeMemoryService()
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
            semantic_dedup_enabled=True,
        )

        # Make _get_embedding return None on the second call (simulates
        # embedding becoming unavailable mid-pipeline).  The try/except
        # around the semantic dedup block in _dedup_candidates must catch
        # any exception and fall back to exact dedup.
        call_count = [0]

        def failing_get(text: str) -> list[float] | None:
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("embedding failed")
            return [0.5, 0.5]

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = failing_get  # type: ignore

        extractor1 = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service.extractor = extractor1
        service.extract_candidates("我喜欢猫", "好的")
        assert len(service.list_pending()) == 1

        # Second call should not crash, even though embedding raises
        extractor2 = FakeExtractor(candidates=[_make_suggestion("我喜欢狗")])
        service.extractor = extractor2
        service.extract_candidates("我喜欢狗", "好的")
        assert len(service.list_pending()) == 2  # both kept


# =====================================================================
# SuggestionService: privacy guard still works
# =====================================================================


class TestPrivacyGuard:
    """Ensure privacy filtering still works with semantic dedup."""

    def test_privacy_guard_runs_before_semantic_dedup(self) -> None:
        """API key candidates should be rejected by privacy guard, not reach semantic dedup."""
        memory = FakeMemoryService()
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
            semantic_dedup_enabled=True,
        )

        # Make embedding always raise (to verify privacy runs first)
        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = lambda text: None  # type: ignore

        extractor = FakeExtractor(candidates=[_make_suggestion("sk-abc123def456ghi789jkl012mno345")])
        service.extractor = extractor
        result = service.extract_candidates("test", "reply")
        assert result.candidates == []
        assert service.list_pending() == []


# =====================================================================
# Regression: P5A extraction unchanged
# =====================================================================


class TestRegressionP5AExtraction:
    """Ensure P5A extraction pipeline is unaffected."""

    def test_extraction_still_works(self) -> None:
        memory = FakeMemoryService()
        extractor = FakeExtractor(candidates=[_make_suggestion("我喜欢猫")])
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=extractor,
        )
        result = service.extract_candidates("我喜欢猫", "好的")
        assert result.status == "success"
        assert len(result.candidates) == 1
        assert len(service.list_pending()) == 1

    def test_extraction_zero_candidates(self) -> None:
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


# =====================================================================
# Config defaults
# =====================================================================


class TestConfigDefaults:
    """Verify config defaults for semantic dedup."""

    def test_config_semantic_dedup_defaults(self) -> None:
        from core.companion_config import CompanionConfig

        cfg = CompanionConfig()
        assert cfg.suggestion.semantic_dedup_enabled is True
        assert cfg.suggestion.semantic_dedup_threshold == 0.85

    def test_config_invalid_threshold_falls_back(self) -> None:
        from core.companion_config import load_companion_config
        from pathlib import Path
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"suggestion": {"semantic_dedup_threshold": 2.0}}')
            f.flush()
            cfg = load_companion_config(f.name)
        assert cfg.suggestion.semantic_dedup_threshold == 0.85


# =====================================================================
# Explicit remember: semantic dedup
# =====================================================================


class TestExplicitRememberSemantic:
    """Ensure handle_explicit_remember also uses semantic dedup."""

    def test_explicit_remember_semantic_duplicate_suppressed(self) -> None:
        """Explicit '记住' with paraphrase of existing record should be suppressed."""
        existing = [
            _make_record("我喜欢机械键盘", MemoryCategory.PREFERENCE)
        ]
        memory = FakeMemoryService(existing_records=existing)
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
            semantic_dedup_enabled=True,
        )

        def fake_get(text: str) -> list[float] | None:
            paraphrase_map = {
                "我喜欢机械键盘": [1.0, 0.0],
                "我比较喜欢用机械键盘": [1.0, 0.0],
            }
            return paraphrase_map.get(text, None)

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        suggestions = service.handle_explicit_remember("请记住我比较喜欢用机械键盘")
        assert suggestions == []
        assert len(service.list_pending()) == 0

    def test_explicit_remember_different_not_suppressed(self) -> None:
        """Explicit '记住' with different content should create pending."""
        existing = [
            _make_record("我喜欢机械键盘", MemoryCategory.PREFERENCE)
        ]
        memory = FakeMemoryService(existing_records=existing)
        service = SuggestionService(
            memory,
            auto_extract_enabled=True,
            extractor=None,
            semantic_dedup_enabled=True,
        )

        def fake_get(text: str) -> list[float] | None:
            paraphrase_map = {
                "我喜欢机械键盘": [1.0, 0.0],
                "我喜欢薄膜键盘": [0.0, 1.0],  # different vector
            }
            return paraphrase_map.get(text, None)

        if service.semantic_dedup:
            service.semantic_dedup._get_embedding = fake_get  # type: ignore

        suggestions = service.handle_explicit_remember("请记住我喜欢薄膜键盘")
        assert len(suggestions) == 1
        assert len(service.list_pending()) == 1
