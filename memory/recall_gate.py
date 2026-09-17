"""Memory Recall Gate (M3B.3) — deterministic inject/don't-inject policy.

Sits AFTER the relevance threshold in ``retrieve_for_prompt`` and answers a
second, orthogonal question: *should this query get memories at all?*

- 0.45 semantic threshold asks: "is this candidate relevant?"
- this gate asks:  "does this QUERY warrant remembering?"

Pure rule engine — no LLM, no second embedding call, no new model.

Band boundary: HIGH_BAND = 0.50 — the only value that sits above BOTH
embedder scales' unrelated noise floors (mapped-lexical ≈ 0.28, real bge
chitchat ceiling ≈ 0.473) while staying below their related bands
(≈ 0.55–0.80).  Frozen; NOT the relevance threshold (0.45, untouched).

Rules (first match wins):
1. **Explicit recall request** (之前/上次/继续/那个…/还记得/我们讨论…) —
   allow: the user is reaching for the past; threshold-passing is enough.
2. **Smalltalk protection** (你好/谢谢/哈哈/今天怎么样/随便聊聊/天气/笑话…) —
   block regardless of scores.
3. **Creation/assistance intent** (帮我设计/帮我写/设计一个…) — the user
   asks for NEW output, not the diary; only a HIGH match may surface.
4. **Knowledge Q&A** (什么是…/解释…/介绍一下…) — same: only HIGH.
5. **Ordinary queries** — Rule 4 kind policy + Rule 5 consistency:
   allow with ≥1 HIGH candidate (any kind), OR ≥2 MEDIUM candidates
   (any kinds — consistency is the signal), OR a single MEDIUM candidate
   of an easy kind (project_context / goal — "较容易召回").
   A single MEDIUM strict-kind candidate (profile_fact / preference /
   episodic / temporary_state / other) is blocked — one weak match must
   not pollute.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

from .m3b import MIN_RELEVANCE_SCORE

HIGH_BAND = 0.50

# Rule 4: kinds that qualify as the single MEDIUM evidence
_EASY_KINDS = frozenset({"project_context", "goal"})

_EXPLICIT_MARKERS = (
    "之前", "上次", "上回", "继续", "还记得", "我们讨论", "讨论过",
    "那个", "那篇", "那次", "刚才说", "你说过",
    "我最近在做什么", "最近在做什么", "我在做什么", "在做什么项目",
    "做什么项目", "我的项目", "最近做了", "你知道我", "工作方式",
    "最近在忙", "最近的进展", "进展如何",
    "last time", "previously", "we discussed", "remember when",
)
_RECALL_WEAK_MARKERS = ("最近", "我们")
_SMALLTALK_MARKERS = (
    "你好", "您好", "谢谢", "多谢", "哈哈", "嘻嘻", "嘿嘿",
    "今天怎么样", "最近怎么样", "随便聊聊", "聊聊天", "在吗",
    "晚安", "早安", "早上好", "再见", "天气", "笑话", "电影",
    "闲聊",
)
_INTENT_MARKERS = (
    "帮我设计", "帮我写", "帮我做", "帮我画", "帮我想",
    "设计一个", "写一个", "做一个", "搭一个", "实现一个",
    "怎么制作", "如何制作",
)
_KNOWLEDGE_MARKERS = (
    "什么是", "什么叫", "解释一下", "解释", "介绍一下", "介绍",
    "是什么意思", "怎么理解", "原理是什么", "的定义", "explain", "what is",
)


class GateDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class RecallDecision:
    """Outcome of the gate: decision, human-readable reason, and the
    subset of candidates that may be injected (empty on BLOCK)."""

    decision: GateDecision
    reason: str
    allowed: tuple[Any, ...] = ()

    @property
    def allowed_ids(self) -> tuple[str, ...]:
        return tuple(
            c.record_id for c in self.allowed
            if getattr(c, "record_id", None) is not None
        )


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


class MemoryRecallGate:
    """Deterministic recall policy.  Stateless; one shared instance is fine."""

    def should_inject(
        self,
        query: str,
        candidates: Sequence[Any],
        context: dict[str, Any] | None = None,
    ) -> RecallDecision:
        """Decide whether ``candidates`` (already past the relevance
        threshold) may be injected for ``query``.

        ``context`` is a reserved dict (conversation_history / mode /
        intent); the current rules use only ``query`` and ``candidates``.
        """
        text = str(query or "")
        if not candidates:
            return RecallDecision(GateDecision.BLOCK,
                                  "no_candidates_above_threshold")

        high = [c for c in candidates
                if getattr(c, "semantic_score", 0.0) >= HIGH_BAND]
        medium = [c for c in candidates
                  if MIN_RELEVANCE_SCORE
                  <= getattr(c, "semantic_score", 0.0) < HIGH_BAND]

        # Rule 1 — explicit recall request dominates everything else
        if _contains_any(text, _EXPLICIT_MARKERS):
            return RecallDecision(
                GateDecision.ALLOW,
                "explicit_reference + candidates_passed_threshold",
                tuple(candidates),
            )

        # Rule 2 — smalltalk protection (no auto injection on chitchat)
        if _contains_any(text, _SMALLTALK_MARKERS):
            return RecallDecision(
                GateDecision.BLOCK, "smalltalk_query_no_memory_injection")

        # Rule 2.5 (v1.1) — 回忆型弱标记 (最近/我们): 有阈值候选即放行。
        # 位于闲聊保护之后: "最近怎么样/我们一起聊聊" 等闲聊仍被 Rule 2 拦截。
        if _contains_any(text, _RECALL_WEAK_MARKERS):
            if high or medium:
                return RecallDecision(
                    GateDecision.ALLOW,
                    "recall_intent_keyword + candidates_passed_threshold",
                    tuple(high) + tuple(medium),
                )

        # Rules 3/4 — creation intent & knowledge Q&A: only a strong
        # semantic match may surface (the user asks for output/facts,
        # not the diary)
        if _contains_any(text, _INTENT_MARKERS) or _contains_any(
                text, _KNOWLEDGE_MARKERS):
            if high:
                return RecallDecision(
                    GateDecision.ALLOW, "intent_or_knowledge + high_score",
                    tuple(high))
            return RecallDecision(
                GateDecision.BLOCK, "intent_or_knowledge + weak_score_only")

        # Rule 5 — ordinary query: consistency over a single weak match
        if high:
            return RecallDecision(
                GateDecision.ALLOW, "high_score_candidate", tuple(high))
        if len(medium) >= 2:
            return RecallDecision(
                GateDecision.ALLOW,
                "multiple_medium_candidates_consistent", tuple(medium))
        if medium:
            only = medium[0]
            if getattr(only, "kind", "other") in _EASY_KINDS:
                return RecallDecision(
                    GateDecision.ALLOW,
                    "medium_easy_kind(project/goal)", (only,))
            return RecallDecision(
                GateDecision.BLOCK, "single_weak_strict_kind_candidate")
        return RecallDecision(
            GateDecision.BLOCK, "no_qualifying_candidates")
