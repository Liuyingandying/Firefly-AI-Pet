"""LLM-based memory candidate extractor for Firefly companion conversations.

This module runs *after* the normal chat reply and is strictly auxiliary:
any failure here is logged and silently degraded — it never blocks or
corrupts the user-visible response.

Design constraints (P5A-1):
- Reuses the existing ConversationProvider abstraction (no new LLM framework).
- Low temperature (0.1) for deterministic structured output.
- Returns 0..N structured candidates matching the existing MemorySuggestion schema.
- Never writes MemoryRecord directly; callers push candidates through
  SuggestionService → pending suggestions.
- Category mapping only uses existing MemoryCategory enum values.
- Parsing failure → empty candidates list (safe no-op).
- Provider unavailable → empty candidates list (safe no-op).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from memory.records import MemoryCategory
from memory.suggestion.memory_candidate_detector import MemorySuggestion

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction prompt — structured, deterministic, user-fact focused.
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT_TEMPLATE = (
    "你是一个记忆分析助手。分析以下对话，提取值得长期记住的信息。"
    "\n\n"
    "【提取规则】"
    "\n- 只提取用户明确表达的事实、偏好、目标或约定。"
    "\n- 不要猜测、推断心理状态、或把助手说的话当用户事实。"
    "\n- 不要提取临时情绪、一次性任务、普通知识问答、当前天气。"
    "\n- 不要提取 API Key、密码、token 等敏感信息。"
    "\n- 如果用户说'记住……'，这一定是高优先级候选。"
    "\n\n"
    "【分类选择】（只能从以下选择）"
    "\n- preference: 用户的明确偏好、喜好"
    "\n- project: 用户的长期目标、计划、持续项目"
    "\n- shared_experience: 重要的共同经历、事件"
    "\n- relationship: 稳定的人物关系事实"
    "\n- user_fact: 用户的稳定事实（身份、习惯等）"
    "\n- emotion: 用户明确表达的稳定情感倾向（非临时情绪）"
    "\n\n"
    "【输出格式】"
    "\n严格输出一个 JSON 数组，每个元素包含："
    "\n- content: 要记住的内容（简洁）"
    "\n- category: 分类（上述之一）"
    "\n- reason: 为什么值得记住（一句话）"
    "\n- evidence: 原文引用（字符串数组）"
    "\n- confidence: 置信度 0.0-1.0"
    "\n\n"
    "如果没有任何值得记住的信息，输出空数组 []。"
    "\n\n"
    "【当前用户消息】"
    "\n{user_message}"
    "\n\n"
    "【助手回复】"
    "\n{assistant_reply}"
    "\n\n"
    "【最近上下文（最多3轮）】"
    "\n{context}"
    "\n\n"
    "请直接输出 JSON，不要任何其他文字。"
)


@dataclass(frozen=True)
class ExtractionResult:
    """Result of running the LLM-based candidate extractor."""

    candidates: list[MemorySuggestion]
    status: str  # "success", "zero_candidates", "parse_failure", "provider_unavailable"

    def __bool__(self) -> bool:
        return len(self.candidates) > 0


class MemoryCandidateExtractor:
    """LLM-based extractor that produces MemorySuggestion candidates from chat turns.

    This extractor is designed to be called *after* the normal chat reply
    completes. It uses a structured prompt with low temperature to produce
    deterministic JSON output matching the MemorySuggestion schema.

    All failures are safely degraded — the extractor never raises to the caller.
    """

    EXTRACTION_MODEL_KWARGS: dict[str, Any] = {
        "temperature": 0.1,
    }

    def __init__(
        self,
        provider: Any,
        *,
        max_candidates: int = 3,
        model: str | None = None,
    ) -> None:
        """
        Args:
            provider: Any object implementing ``chat(messages, model, temperature)``.
                      Typically a ProviderRouter or BaseProvider instance.
            max_candidates: Maximum number of candidates to return per turn.
            model: Optional model override for extraction calls.
        """
        self.provider = provider
        self.max_candidates = max_candidates
        self.model = model

    def extract(
        self,
        user_message: str,
        assistant_reply: str,
        context: Sequence[dict[str, str]] | None = None,
    ) -> ExtractionResult:
        """Run LLM-based extraction on a single chat turn.

        Args:
            user_message: The user's input message.
            assistant_reply: The assistant's reply text.
            context: Optional recent conversation context as message dicts.

        Returns:
            ExtractionResult with 0..N candidates, or empty on any failure.
        """
        if not isinstance(user_message, str) or not user_message.strip():
            return ExtractionResult(
                candidates=[],
                status="zero_candidates",
            )

        try:
            prompt = self._build_prompt(user_message, assistant_reply, context)
            response = self._call_provider(prompt)
            candidates = self._parse_response(response)
        except _ProviderUnavailable:
            logger.debug("Memory candidate extraction: provider unavailable")
            return ExtractionResult(
                candidates=[],
                status="provider_unavailable",
            )
        except _ParseFailure:
            logger.debug("Memory candidate extraction: parse failure")
            return ExtractionResult(
                candidates=[],
                status="parse_failure",
            )
        except Exception as exc:
            logger.debug(
                "Memory candidate extraction failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return ExtractionResult(
                candidates=[],
                status="parse_failure",
            )

        if not candidates:
            return ExtractionResult(
                candidates=[],
                status="zero_candidates",
            )

        return ExtractionResult(
            candidates=candidates[: self.max_candidates],
            status="success",
        )

    def _build_prompt(
        self,
        user_message: str,
        assistant_reply: str,
        context: Sequence[dict[str, str]] | None,
    ) -> str:
        context_text = ""
        if context:
            parts: list[str] = []
            for msg in context[-6:]:  # up to 3 turns (user+assistant each)
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                parts.append(f"[{role}]: {content}")
            context_text = "\n".join(parts)

        return _EXTRACTION_PROMPT_TEMPLATE.format(
            user_message=user_message,
            assistant_reply=assistant_reply,
            context=context_text if context_text else "（无）",
        )

    def _call_provider(self, prompt: str) -> str:
        """Call the underlying provider and return the response text."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "你是记忆分析助手，只输出JSON数组。"},
            {"role": "user", "content": prompt},
        ]
        try:
            response = self.provider.chat(
                messages,
                model=self.model,
                temperature=self.EXTRACTION_MODEL_KWARGS["temperature"],
            )
        except Exception as exc:
            # Re-raise as our internal marker so extract() can distinguish
            raise _ProviderUnavailable(str(exc)) from exc
        return _extract_text_from_response(response)

    def _parse_response(self, raw_text: str) -> list[MemorySuggestion]:
        """Parse the provider's JSON response into MemorySuggestion objects."""
        raw_text = raw_text.strip()
        if not raw_text:
            return []

        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise _ParseFailure(f"JSON parse error: {exc}") from exc

        if not isinstance(parsed, list):
            raise _ParseFailure("response is not a JSON array")

        candidates: list[MemorySuggestion] = []
        for item in parsed:
            if not isinstance(item, Mapping):
                continue
            try:
                suggestion = _parse_suggestion_item(item)
                if suggestion is not None:
                    candidates.append(suggestion)
            except Exception:
                # Skip malformed items, don't fail the whole parse
                continue

        return candidates


def _parse_suggestion_item(item: Mapping[str, Any]) -> MemorySuggestion | None:
    """Parse a single JSON object into a MemorySuggestion."""
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        return None

    category_raw = item.get("category")
    try:
        category = MemoryCategory(category_raw)
    except (TypeError, ValueError):
        return None

    reason = item.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)

    evidence_raw = item.get("evidence", [])
    if isinstance(evidence_raw, list):
        evidence = tuple(str(e) for e in evidence_raw if e)
    elif isinstance(evidence_raw, str):
        evidence = (evidence_raw,)
    else:
        evidence = ()

    confidence_raw = item.get("confidence", 0.5)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return MemorySuggestion(
        content=content.strip(),
        category=category,
        reason=reason.strip() if reason else "",
        evidence=evidence,
        confidence=confidence,
    )


def _extract_text_from_response(response: Mapping[str, Any]) -> str:
    """Extract text content from an OpenAI-compatible chat completion response."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return ""
    message = choice.get("message")
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    return ""


class ProviderUnavailableError(Exception):
    """Raised when the extraction provider is unavailable."""


class _ProviderUnavailable(Exception):
    """Internal marker for provider unavailability during extraction."""


class _ParseFailure(Exception):
    """Internal marker for parse failure during extraction."""


__all__ = [
    "ExtractionResult",
    "MemoryCandidateExtractor",
    "ProviderUnavailableError",
]
