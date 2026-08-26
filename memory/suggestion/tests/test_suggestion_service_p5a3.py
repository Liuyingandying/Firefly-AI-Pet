"""P5A-3: Limited Auto-Approve for Explicit Remember Requests — unit tests.

Tests cover:
- Eligibility: explicit remember + allowed category + high confidence
- Confidence threshold
- Non-explicit candidates (auto-extract) → never auto-approved
- Category allowlist
- Feature disabled
- Privacy / Security guards (API key, password, JWT/Bearer)
- Exact dedup
- Semantic dedup (Pending + Saved Memory)
- Write path verification (uses existing accept → MemoryService.remember)
- No direct repository bypass
- Safe degradation (Mem0/repository error)
- UI regression (auto-approved item not in pending list)
- Runtime: explicit remember vs ordinary candidate vs secret remember
"""

from __future__ import annotations

from typing import Any

import pytest

from memory.records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy
from memory.suggestion.suggestion_service import SuggestionService
from memory.suggestion.memory_candidate_detector import MemorySuggestion
from memory.security_guard import (
    GuardDecision,
    MemorySecurityGuard,
)


# ====================================================================== Fixtures
class FakeMemoryService:
    """Minimal fake that mimics MemoryService for suggestion tests."""

    def __init__(
        self,
        fail: bool = False,
        records: list[MemoryRecord] | None = None,
    ) -> None:
        self.remembered: list[dict] = []
        self.fail = fail
        self._records: list[MemoryRecord] = records if records is not None else []

    def remember(
        self,
        user_input: str,
        *,
        category: str | MemoryCategory | None = None,
        trigger: str = "explicit-command",
        permission: Any = None,
        asserted_explicit: bool = False,
    ) -> MemoryRecord | None:
        if self.fail:
            raise RuntimeError("write failed")
        cat = MemoryCategory(category) if category else MemoryCategory.PREFERENCE
        record = MemoryRecord.create(
            category=cat,
            content=user_input.strip() if isinstance(user_input, str) else str(user_input),
            trigger=trigger,
            source=MemorySource.EXPLICIT,
            permission=WritePolicy.EXPLICIT_ONLY,
        )
        self.remembered.append({
            "content": record.content,
            "category": record.category.value,
            "trigger": record.trigger,
        })
        self._records.append(record)
        return record

    def list(
        self,
        category: MemoryCategory | str | None = None,
        source: MemorySource | str | None = None,
        before_ts: int | None = None,
    ) -> list[MemoryRecord]:
        records = self._records
        if category is not None:
            cat_enum = MemoryCategory(category) if isinstance(category, str) else category
            records = [r for r in records if r.category == cat_enum]
        if source is not None:
            src_enum = MemorySource(source) if isinstance(source, str) else source
            records = [r for r in records if r.source == src_enum]
        if before_ts is not None:
            records = [r for r in records if r.created_ts < before_ts]
        return records


class _BlockingSecurityGuard:
    """Security guard that always blocks."""

    def guard(self, content: str) -> GuardDecision:
        return GuardDecision(action="block", reason="blocked by test guard")


class _PassThroughSecurityGuard:
    """Security guard that always allows."""

    def guard(self, content: str) -> GuardDecision:
        return GuardDecision(action="allow")


class _RedactingSecurityGuard:
    """Security guard that blocks content containing 'secret'."""

    def guard(self, content: str) -> GuardDecision:
        if "secret" in content.lower() or "api key" in content.lower():
            return GuardDecision(action="block", reason="contains secret")
        return GuardDecision(action="allow")


def _make_suggestion_service(
    memory_service: FakeMemoryService | None = None,
    *,
    explicit_auto_approve_enabled: bool = False,
    explicit_auto_approve_min_confidence: float = 0.95,
    explicit_auto_approve_allowed_categories: list[str] | None = None,
    security_guard: MemorySecurityGuard | None = None,
    semantic_dedup_enabled: bool = True,
) -> SuggestionService:
    """Create a SuggestionService with configurable auto-approve settings."""
    ms = memory_service or FakeMemoryService()
    return SuggestionService(
        ms,
        enabled=True,
        auto_extract_enabled=False,
        explicit_auto_approve_enabled=explicit_auto_approve_enabled,
        explicit_auto_approve_min_confidence=explicit_auto_approve_min_confidence,
        explicit_auto_approve_allowed_categories=explicit_auto_approve_allowed_categories,
        security_guard=security_guard,
        semantic_dedup_enabled=semantic_dedup_enabled,
    )


def _make_explicit_suggestion(
    content: str,
    category: MemoryCategory = MemoryCategory.PREFERENCE,
    confidence: float = 0.95,
) -> MemorySuggestion:
    """Create a mock explicit_remember suggestion."""
    return MemorySuggestion(
        content=content,
        category=category,
        reason="explicit_remember",
        evidence=("请记住，" + content,),
        confidence=confidence,
    )


# ================================================================= Eligibility
class TestEligibility:
    """Auto-approve eligibility conditions."""

    def test_explicit_remember_allowed_category_high_confidence_auto_approves(
        self,
    ):
        """explicit remember + allowed category + confidence >= 0.95 → auto-approved."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        result = svc.handle_explicit_remember(
            "请记住，我喜欢 75% 配列键盘。"
        )
        # Should be auto-approved → not pending
        assert len(result) == 0
        assert len(svc.list_pending()) == 0
        # Should be written to memory
        assert len(ms.remembered) == 1
        assert "75% 配列键盘" in ms.remembered[0]["content"]

    def test_confidence_below_threshold_stays_pending(self):
        """confidence < 0.95 → stays as Pending."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            explicit_auto_approve_min_confidence=0.99,
        )
        result = svc.handle_explicit_remember(
            "请记住，我喜欢机械键盘。",
            confidence=0.95,
        )
        assert len(result) == 1
        assert len(svc.list_pending()) == 1
        # Should NOT be written
        assert len(ms.remembered) == 0

    def test_non_explicit_high_confidence_stays_pending(self):
        """Non-explicit (auto-extracted) high-confidence candidate → Pending."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        # Create a non-explicit suggestion directly
        suggestion = MemorySuggestion(
            content="我喜欢机械键盘",
            category=MemoryCategory.PREFERENCE,
            reason="stable_preference",  # NOT explicit_remember
            evidence=("我喜欢机械键盘",),
            confidence=1.0,
        )
        svc._pending.append(suggestion)
        # Auto-approve should NOT fire
        auto_approved = svc._try_auto_approve(suggestion)
        assert auto_approved is False
        assert len(svc.list_pending()) == 1

    def test_disallowed_category_stays_pending(self):
        """Category not in allowlist → Pending."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            explicit_auto_approve_allowed_categories=[MemoryCategory.PREFERENCE.value],
        )
        # USER_FACT is not in allowlist
        result = svc.handle_explicit_remember(
            "请记住，我是北京人。",
        )
        # infer_category maps "北京人" to USER_FACT (no keyword match)
        # So it should stay as pending
        assert len(result) == 1
        assert len(svc.list_pending()) == 1

    def test_feature_disabled_stays_pending(self):
        """Feature disabled → all explicit remembers stay Pending."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=False,
        )
        result = svc.handle_explicit_remember(
            "请记住，我喜欢 Python。"
        )
        assert len(result) == 1
        assert len(svc.list_pending()) == 1
        assert len(ms.remembered) == 0

    def test_default_allowlist_includes_preference_and_project(self):
        """Default allowed categories are preference and project."""
        svc = _make_suggestion_service()
        assert MemoryCategory.PREFERENCE.value in svc.explicit_auto_approve_allowed_categories
        assert MemoryCategory.PROJECT.value in svc.explicit_auto_approve_allowed_categories


# =============================================================== Privacy / Security
class TestPrivacySecurity:
    """Secrets must never be auto-approved."""

    def test_explicit_api_key_never_auto_approved(self):
        """Explicit API key → blocked by security guard, stays pending or suppressed."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            security_guard=_BlockingSecurityGuard(),
        )
        result = svc.handle_explicit_remember(
            "请记住我的 API key 是 sk-abcdefghij1234567890"
        )
        # Should be blocked by security guard → not auto-approved
        assert len(ms.remembered) == 0

    def test_explicit_password_never_auto_approved(self):
        """Explicit password → blocked."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            security_guard=_BlockingSecurityGuard(),
        )
        result = svc.handle_explicit_remember(
            "请记住我的密码是 mysupersecretpassword123"
        )
        assert len(ms.remembered) == 0

    def test_explicit_jwt_never_auto_approved(self):
        """JWT / Bearer token → blocked."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            security_guard=_BlockingSecurityGuard(),
        )
        result = svc.handle_explicit_remember(
            "请记住我的 JWT 是 eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        assert len(ms.remembered) == 0

    def test_redacting_guard_blocks_secret_content(self):
        """Redacting security guard blocks content with 'secret'."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            security_guard=_RedactingSecurityGuard(),
        )
        # This content should be blocked by the redacting guard
        suggestion = _make_explicit_suggestion("我的 secret 是 abcdef")
        result = svc._try_auto_approve(suggestion)
        assert result is False


# =================================================================== Dedup
class TestDedup:
    """Dedup must prevent auto-approve of duplicates."""

    def test_exact_duplicate_no_new_record(self):
        """Exact duplicate → suppressed, no auto-write."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        # First remember
        svc.handle_explicit_remember("请记住，我喜欢机械键盘。")
        assert len(ms.remembered) == 1
        # Second identical remember → suppressed
        result = svc.handle_explicit_remember("请记住，我喜欢机械键盘。")
        assert len(result) == 0  # suppressed
        assert len(ms.remembered) == 1  # count unchanged

    def test_semantic_pending_duplicate_no_auto_write(self):
        """Semantic duplicate of pending suggestion → suppressed by _dedup_candidates."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            semantic_dedup_enabled=True,
        )
        # Add a pending suggestion with similar content
        pending = MemorySuggestion(
            content="我喜欢机械键盘",
            category=MemoryCategory.PREFERENCE,
            reason="stable_preference",
            evidence=("我喜欢机械键盘",),
            confidence=0.8,
        )
        svc._pending.append(pending)
        # _dedup_candidates should suppress semantic duplicates
        # Create a candidate with similar content
        candidate = MemorySuggestion(
            content="我比较喜欢用机械键盘",
            category=MemoryCategory.PREFERENCE,
            reason="llm_extraction",
            evidence=("我比较喜欢用机械键盘",),
            confidence=0.9,
        )
        deduped = svc._dedup_candidates([candidate])
        # Should be suppressed by semantic dedup
        assert len(deduped) == 0

    def test_semantic_saved_memory_duplicate_no_auto_write(self):
        """Semantic duplicate of existing MemoryRecord → suppressed."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            semantic_dedup_enabled=True,
        )
        # Pre-populate with a similar record
        record = MemoryRecord.create(
            category=MemoryCategory.PREFERENCE,
            content="我喜欢机械键盘",
            trigger="manual",
            source=MemorySource.EXPLICIT,
            permission=WritePolicy.EXPLICIT_ONLY,
        )
        ms._records.append(record)
        # Try to explicit remember similar content
        result = svc.handle_explicit_remember("请记住，我比较喜欢用机械键盘。")
        # Should be suppressed by semantic dedup against existing record
        assert len(result) == 0


# ================================================================ Write path
class TestWritePath:
    """Auto-approve must use existing accept() path, never direct repo write."""

    def test_eligible_suggestion_auto_approves_via_accept(self):
        """Eligible suggestion → MemoryService.remember() → Repository contains one record."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        result = svc.handle_explicit_remember(
            "请记住，我使用 Ubuntu 22.04 开发。"
        )
        assert len(result) == 0
        assert len(svc.list_pending()) == 0
        records = ms.list()
        assert len(records) == 1
        # Verify it went through the normal path
        assert records[0].source == MemorySource.EXPLICIT
        # accept() uses trigger=f"suggestion:{suggestion.reason}"
        assert records[0].trigger == "suggestion:explicit_remember"

    def test_no_direct_repository_bypass(self):
        """Auto-approve should not bypass MemoryService.remember()."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        initial_count = len(ms.remembered)
        svc.handle_explicit_remember("请记住，我喜欢喝茶。")
        final_count = len(ms.remembered)
        # Only one record should exist (from auto-approve)
        assert final_count == initial_count + 1

    def test_mem0_unavailable_safe_degradation(self):
        """If write fails, suggestion stays pending (safe degradation)."""
        ms = FakeMemoryService(fail=True)
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        # Manually create a suggestion and add to pending, then try auto-approve
        suggestion = _make_explicit_suggestion("我喜欢咖啡")
        svc._pending.append(suggestion)
        result = svc._try_auto_approve(suggestion)
        # Write failed → accept() returns None → _try_auto_approve returns False
        assert result is False
        # Suggestion should still be in pending (accept() didn't remove it)
        assert len(svc.list_pending()) == 1
        assert len(ms.remembered) == 0


# ================================================================ UI regression
class TestUIRegression:
    """Auto-approved items should not appear in Pending UI."""

    def test_auto_approved_item_not_in_pending(self):
        """Auto-approved suggestion does not remain Pending."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        svc.handle_explicit_remember("请记住，我喜欢 Python。")
        assert len(svc.list_pending()) == 0

    def test_ordinary_pending_item_still_appears(self):
        """Ordinary Pending item still appears."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        # Ordinary detect (not explicit remember)
        svc.detect("我喜欢猫")
        assert len(svc.list_pending()) == 1

    def test_reject_approve_manual_behavior_unchanged(self):
        """Manual reject/approve behavior unchanged."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        svc.detect("我喜欢狗")
        assert len(svc.list_pending()) == 1
        suggestion = svc.list_pending()[0]
        # Reject
        svc.reject(suggestion)
        assert len(svc.list_pending()) == 0
        # No record written
        assert len(ms.remembered) == 0

        # Approve
        svc.detect("我喜欢鸟")
        assert len(svc.list_pending()) == 1
        suggestion = svc.list_pending()[0]
        svc.accept(suggestion)
        assert len(svc.list_pending()) == 0
        assert len(ms.remembered) == 1


# ================================================================= Runtime
class TestRuntime:
    """Runtime integration: explicit remember vs ordinary vs secret."""

    def test_explicit_remember_creates_record_when_enabled(self):
        """'请记住，我喜欢用 pytest 写 Python 测试。' → MemoryRecord created (preference category)."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        svc.handle_explicit_remember(
            "请记住，我喜欢用 pytest 写 Python 测试。"
        )
        records = ms.list()
        assert len(records) == 1
        # Category should be PREFERENCE (matches "喜欢" keyword)
        assert records[0].category == MemoryCategory.PREFERENCE

    def test_ordinary_candidate_stays_pending(self):
        """'我喜欢猫' → Pending only (matched by stable_preference pattern)."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        svc.detect("我喜欢猫")
        assert len(svc.list_pending()) == 1
        assert len(ms.remembered) == 0

    def test_secret_explicit_remember_creates_zero_records(self):
        """Secret explicit remember → 0 MemoryRecord."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            security_guard=_BlockingSecurityGuard(),
        )
        result = svc.handle_explicit_remember(
            "请记住我的 API key 是 sk-test-not-real-1234567890"
        )
        assert len(ms.remembered) == 0

    def test_non_explicit_high_confidence_not_auto_approved(self):
        """Non-explicit candidate with confidence 1.0 → Pending, not auto-approved."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        suggestion = MemorySuggestion(
            content="我的长期目标是成为全栈工程师",
            category=MemoryCategory.PROJECT,
            reason="long_term_goal",
            evidence=("我的长期目标是成为全栈工程师",),
            confidence=1.0,
        )
        result = svc._try_auto_approve(suggestion)
        assert result is False
        assert len(svc.list_pending()) == 0  # not added to pending by _try_auto_approve


# =========================================================== Config validation
class TestConfigValidation:
    """Configuration field validation."""

    def test_default_auto_approve_disabled(self):
        """Default explicit_auto_approve_enabled is False."""
        svc = _make_suggestion_service()
        assert svc.explicit_auto_approve_enabled is False

    def test_default_min_confidence_0_95(self):
        """Default explicit_auto_approve_min_confidence is 0.95."""
        svc = _make_suggestion_service()
        assert svc.explicit_auto_approve_min_confidence == 0.95

    def test_custom_allowed_categories(self):
        """Custom allowed categories are respected."""
        svc = _make_suggestion_service(
            explicit_auto_approve_allowed_categories=[MemoryCategory.USER_FACT.value]
        )
        assert MemoryCategory.USER_FACT.value in svc.explicit_auto_approve_allowed_categories
        assert MemoryCategory.PREFERENCE.value not in svc.explicit_auto_approve_allowed_categories


# =========================================================== SuggestionSettings config
class TestSuggestionSettingsConfig:
    """Test that SuggestionSettings correctly loads P5A-3 fields."""

    def test_suggestion_settings_has_auto_approve_fields(self):
        """SuggestionSettings has explicit_auto_approve_enabled field."""
        from core.companion_config import SuggestionSettings
        settings = SuggestionSettings()
        assert hasattr(settings, "explicit_auto_approve_enabled")
        assert settings.explicit_auto_approve_enabled is False
        assert settings.explicit_auto_approve_min_confidence == 0.95
        assert MemoryCategory.PREFERENCE.value in settings.explicit_auto_approve_allowed_categories
        assert MemoryCategory.PROJECT.value in settings.explicit_auto_approve_allowed_categories

    def test_suggestion_settings_loads_from_dict(self):
        """SuggestionSettings loads custom values from dict."""
        from core.companion_config import SuggestionSettings
        settings = SuggestionSettings(
            explicit_auto_approve_enabled=True,
            explicit_auto_approve_min_confidence=0.90,
            explicit_auto_approve_allowed_categories=["user_fact"],
        )
        assert settings.explicit_auto_approve_enabled is True
        assert settings.explicit_auto_approve_min_confidence == 0.90
        assert "user_fact" in settings.explicit_auto_approve_allowed_categories
        assert "preference" not in settings.explicit_auto_approve_allowed_categories


# =========================================================== _try_auto_approve edge cases
class TestTryAutoApproveEdgeCases:
    """Edge cases for _try_auto_approve."""

    def test_try_auto_approve_returns_false_when_feature_disabled(self):
        """_try_auto_approve returns False when feature disabled."""
        svc = _make_suggestion_service(
            explicit_auto_approve_enabled=False,
        )
        suggestion = _make_explicit_suggestion("test")
        result = svc._try_auto_approve(suggestion)
        assert result is False

    def test_try_auto_approve_returns_false_for_non_explicit_reason(self):
        """_try_auto_approve returns False for non-explicit_remember reason."""
        svc = _make_suggestion_service(
            explicit_auto_approve_enabled=True,
        )
        suggestion = MemorySuggestion(
            content="test",
            category=MemoryCategory.PREFERENCE,
            reason="llm_extraction",
            evidence=("test",),
            confidence=1.0,
        )
        result = svc._try_auto_approve(suggestion)
        assert result is False

    def test_try_auto_approve_allows_when_all_conditions_met(self):
        """_try_auto_approve returns True when all conditions met."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
        )
        suggestion = _make_explicit_suggestion("我喜欢阅读")
        result = svc._try_auto_approve(suggestion)
        assert result is True
        assert len(ms.remembered) == 1

    def test_try_auto_approve_blocks_on_security_guard_block(self):
        """_try_auto_approve returns False when security guard blocks."""
        ms = FakeMemoryService()
        svc = _make_suggestion_service(
            ms,
            explicit_auto_approve_enabled=True,
            security_guard=_BlockingSecurityGuard(),
        )
        suggestion = _make_explicit_suggestion("test")
        result = svc._try_auto_approve(suggestion)
        assert result is False
