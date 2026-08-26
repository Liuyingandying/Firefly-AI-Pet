"""Tests for the LLM-based memory candidate extractor."""

from __future__ import annotations

from typing import Any

import pytest

from memory.records import MemoryCategory
from memory.suggestion.candidate_extractor import (
    ExtractionResult,
    MemoryCandidateExtractor,
    _extract_text_from_response,
    _parse_suggestion_item,
)
from memory.suggestion.memory_candidate_detector import MemorySuggestion


class FakeProvider:
    """Fake provider that returns pre-configured responses."""

    def __init__(self, response_text: str = "[]", fail: bool = False) -> None:
        self.response_text = response_text
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.calls.append({"messages": messages, "model": model, "temperature": temperature})
        if self.fail:
            raise RuntimeError("provider unavailable")
        return {
            "choices": [{"message": {"role": "assistant", "content": self.response_text}}],
        }


class TestParseSuggestionItem:
    """Tests for _parse_suggestion_item."""

    def test_valid_item(self) -> None:
        item = {
            "content": "我喜欢喝咖啡",
            "category": "preference",
            "reason": "stable preference",
            "evidence": ["我平时喜欢喝咖啡"],
            "confidence": 0.8,
        }
        result = _parse_suggestion_item(item)
        assert result is not None
        assert result.content == "我喜欢喝咖啡"
        assert result.category is MemoryCategory.PREFERENCE
        assert result.reason == "stable preference"
        assert result.evidence == ("我平时喜欢喝咖啡",)
        assert result.confidence == 0.8

    def test_empty_content_returns_none(self) -> None:
        assert _parse_suggestion_item({"content": ""}) is None
        assert _parse_suggestion_item({"content": "   "}) is None
        assert _parse_suggestion_item({}) is None

    def test_invalid_category_returns_none(self) -> None:
        assert _parse_suggestion_item({"content": "test", "category": "invalid"}) is None

    def test_missing_evidence_defaults_to_empty_tuple(self) -> None:
        result = _parse_suggestion_item({
            "content": "test",
            "category": "preference",
        })
        assert result is not None
        assert result.evidence == ()

    def test_evidence_as_string(self) -> None:
        result = _parse_suggestion_item({
            "content": "test",
            "category": "preference",
            "evidence": "single evidence",
        })
        assert result is not None
        assert result.evidence == ("single evidence",)

    def test_confidence_clamped_to_0_1(self) -> None:
        result = _parse_suggestion_item({
            "content": "test",
            "category": "preference",
            "confidence": 1.5,
        })
        assert result is not None
        assert result.confidence == 1.0

        result2 = _parse_suggestion_item({
            "content": "test",
            "category": "preference",
            "confidence": -0.5,
        })
        assert result2 is not None
        assert result2.confidence == 0.0


class TestExtractTextFromResponse:
    """Tests for _extract_text_from_response."""

    def test_normal_response(self) -> None:
        response = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
        assert _extract_text_from_response(response) == "hello"

    def test_empty_choices(self) -> None:
        assert _extract_text_from_response({"choices": []}) == ""

    def test_missing_choices(self) -> None:
        assert _extract_text_from_response({}) == ""

    def test_missing_message(self) -> None:
        assert _extract_text_from_response({"choices": [{}]}) == ""

    def test_missing_content(self) -> None:
        assert _extract_text_from_response({"choices": [{"message": {}}]}) == ""


class TestMemoryCandidateExtractor:
    """Integration tests for MemoryCandidateExtractor."""

    def test_empty_response_returns_zero_candidates(self) -> None:
        provider = FakeProvider(response_text="[]")
        extractor = MemoryCandidateExtractor(provider)
        result = extractor.extract("你好", "你好！有什么可以帮助你的？")
        assert result.status == "zero_candidates"
        assert result.candidates == []

    def test_valid_candidates_parsed(self) -> None:
        json_response = '[{"content": "我喜欢喝咖啡", "category": "preference", "reason": "偏好", "evidence": ["我喜欢喝咖啡"], "confidence": 0.9}]'
        provider = FakeProvider(response_text=json_response)
        extractor = MemoryCandidateExtractor(provider)
        result = extractor.extract("我喜欢喝咖啡", "好的")
        assert result.status == "success"
        assert len(result.candidates) == 1
        assert result.candidates[0].content == "我喜欢喝咖啡"
        assert result.candidates[0].category is MemoryCategory.PREFERENCE
        assert result.candidates[0].confidence == 0.9

    def test_malformed_json_returns_empty(self) -> None:
        provider = FakeProvider(response_text="not json at all {{{")
        extractor = MemoryCandidateExtractor(provider)
        result = extractor.extract("test", "reply")
        assert result.status == "parse_failure"
        assert result.candidates == []

    def test_provider_unavailable_returns_safe_noop(self) -> None:
        provider = FakeProvider(fail=True)
        extractor = MemoryCandidateExtractor(provider)
        result = extractor.extract("test", "reply")
        assert result.status == "provider_unavailable"
        assert result.candidates == []

    def test_empty_user_message_returns_zero(self) -> None:
        provider = FakeProvider(response_text='[{"content": "test"}]')
        extractor = MemoryCandidateExtractor(provider)
        result = extractor.extract("", "reply")
        assert result.status == "zero_candidates"
        assert result.candidates == []

    def test_max_candidates_capped(self) -> None:
        many = [{"content": f"item{i}", "category": "preference", "reason": "r", "evidence": [], "confidence": 0.5} for i in range(10)]
        provider = FakeProvider(response_text=str(many).replace("'", '"'))
        extractor = MemoryCandidateExtractor(provider, max_candidates=3)
        result = extractor.extract("test", "reply")
        assert len(result.candidates) <= 3

    def test_temperature_is_low(self) -> None:
        provider = FakeProvider(response_text="[]")
        extractor = MemoryCandidateExtractor(provider)
        extractor.extract("test", "reply")
        assert len(provider.calls) == 1
        assert provider.calls[0]["temperature"] == 0.1

    def test_context_passed_to_provider(self) -> None:
        provider = FakeProvider(response_text="[]")
        extractor = MemoryCandidateExtractor(provider)
        context = [{"role": "user", "content": "之前说过的事"}, {"role": "assistant", "content": "记得"}]
        extractor.extract("test", "reply", context=context)
        assert len(provider.calls) == 1
        # The prompt should contain context text
        user_msg = provider.calls[0]["messages"][1]["content"]
        assert "之前说过的事" in user_msg

    def test_non_list_json_returns_empty(self) -> None:
        provider = FakeProvider(response_text='{"not": "array"}')
        extractor = MemoryCandidateExtractor(provider)
        result = extractor.extract("test", "reply")
        assert result.status == "parse_failure"
        assert result.candidates == []

    def test_mixed_valid_invalid_items(self) -> None:
        # One valid, one invalid (no content), one valid
        json_response = (
            '[{"content": "valid1", "category": "preference", "reason": "r", "evidence": [], "confidence": 0.5}, '
            '{"category": "preference", "reason": "r", "evidence": [], "confidence": 0.5}, '
            '{"content": "valid2", "category": "project", "reason": "r", "evidence": [], "confidence": 0.7}]'
        )
        provider = FakeProvider(response_text=json_response)
        extractor = MemoryCandidateExtractor(provider)
        result = extractor.extract("test", "reply")
        assert result.status == "success"
        assert len(result.candidates) == 2
        assert result.candidates[0].content == "valid1"
        assert result.candidates[1].content == "valid2"

    def test_bool_result(self) -> None:
        provider = FakeProvider(response_text="[]")
        extractor = MemoryCandidateExtractor(provider)
        empty_result = extractor.extract("test", "reply")
        assert not empty_result

        json_response = '[{"content": "test", "category": "preference", "reason": "r", "evidence": [], "confidence": 0.5}]'
        provider2 = FakeProvider(response_text=json_response)
        extractor2 = MemoryCandidateExtractor(provider2)
        filled_result = extractor2.extract("test", "reply")
        assert filled_result


class TestExplicitRemember:
    """Tests for explicit '记住' request handling via extractor."""

    def test_explicit_remember_in_user_message(self) -> None:
        """When user says '记住...' the extractor should pick it up."""
        json_response = (
            '[{"content": "用户叫小明", "category": "user_fact", '
            '"reason": "explicit_remember", "evidence": ["请记住用户叫小明"], "confidence": 0.95}]'
        )
        provider = FakeProvider(response_text=json_response)
        extractor = MemoryCandidateExtractor(provider)
        result = extractor.extract("请记住用户叫小明", "好的，我会记住的")
        assert result.status == "success"
        assert len(result.candidates) == 1
        assert result.candidates[0].reason == "explicit_remember"
