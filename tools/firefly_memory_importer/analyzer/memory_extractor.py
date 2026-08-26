"""Deterministic memory extraction from parsed history (Stage 1.2).

Pure Python: no ``core``/``memory``/``character`` imports and no LLM. Extracts
``MemoryCandidate`` items from ``ParsedHistory`` by applying category rules to
user turns only. Assistant turns are never fact sources (R1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Conversation, ParsedHistory, Role, Turn
from .attribution import has_ooc_marker, is_roleplay_heavy
from .candidates import MemoryCandidate, MemoryCategory, SourceRef
from .confidence import (
    has_explicit_marker,
    has_hedging,
    has_specific_detail,
    memory_confidence,
)


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    category: MemoryCategory
    pattern: re.Pattern[str]


_RULES = (
    _Rule(
        "mem.user_fact.name",
        MemoryCategory.USER_FACT,
        re.compile(r"(?:我叫|我的名字是|我的名字叫|名字叫)\s*(?P<content>[^\s。，,]{1,20})"),
    ),
    _Rule(
        "mem.user_fact.occupation",
        MemoryCategory.USER_FACT,
        re.compile(r"我(?:是|是一名|是一个|是个)\s*(?P<content>[一-龥]{2,10})"),
    ),
    _Rule(
        "mem.user_fact.location",
        MemoryCategory.USER_FACT,
        re.compile(r"(?:我在|我住在|我来自)\s*(?P<content>[一-龥]{2,10})"),
    ),
    _Rule(
        "mem.preference.nickname",
        MemoryCategory.PREFERENCE,
        re.compile(r"(?:叫我|称呼我|喊我)\s*(?P<content>[一-龥A-Za-z]{1,10})"),
    ),
    _Rule(
        "mem.preference.verbosity",
        MemoryCategory.PREFERENCE,
        re.compile(
            r"(?:回复|说话|回答)\s*(?P<content>(?:短|长|简洁|简短|详细)一点|(?:短|长|简洁|简短|详细)些)"
        ),
    ),
    _Rule(
        "mem.project.context",
        MemoryCategory.PROJECT,
        re.compile(
            r"(?:我在|我们)\s*(?:开发|做|写|搞|研究|搭建|实现)\s*(?P<content>.+?)(?:[。！!？?]|$)"
        ),
    ),
    _Rule(
        "mem.emotion.state",
        MemoryCategory.EMOTION,
        re.compile(
            r"我(?:最近|现在|这段时间)?"
            r"(?P<content>(?:很|有点|挺|特别|非常)?(?:开心|难过|焦虑|累|沮丧|高兴|烦躁|紧张|低落))"
        ),
    ),
    _Rule(
        "mem.experience.together",
        MemoryCategory.SHARED_EXPERIENCE,
        re.compile(
            r"(?:我们一起|我们共同|我们)\s*(?:完成|做|经历|达成|发布|写完|搞定)(?:了)?"
            r"(?P<content>.+?)(?:[。！!？?]|$)"
        ),
    ),
)


_SENSITIVE_CATEGORIES = frozenset({MemoryCategory.EMOTION})


@dataclass
class _Draft:
    category: MemoryCategory
    content: str
    rule_id: str
    refs: list[SourceRef] = field(default_factory=list)
    roleplay: bool = False
    explicit: bool = False
    hedged: bool = False
    specific: bool = False


class MemoryExtractor:
    """Extract ``MemoryCandidate`` items from user turns of a ``ParsedHistory``."""

    def extract(self, history: ParsedHistory) -> list[MemoryCandidate]:
        drafts: dict[tuple[MemoryCategory, str], _Draft] = {}
        for conversation in history.conversations:
            roleplay_heavy = is_roleplay_heavy(conversation)
            for turn in conversation.turns:
                if turn.role is not Role.USER:
                    continue
                self._collect(conversation, turn, roleplay_heavy, drafts)

        return [
            self._build_candidate(f"m-{index:04d}", draft)
            for index, draft in enumerate(drafts.values(), start=1)
        ]

    def _collect(
        self,
        conversation: Conversation,
        turn: Turn,
        roleplay_heavy: bool,
        drafts: dict[tuple[MemoryCategory, str], _Draft],
    ) -> None:
        ref = SourceRef(
            conversation_id=conversation.id,
            turn_seq=turn.seq,
            role=turn.role,
            ts=turn.ts,
            message_id=turn.message_id,
        )
        roleplay = roleplay_heavy and not has_ooc_marker(turn.content)
        explicit = has_explicit_marker(turn.content)
        hedged = has_hedging(turn.content)

        for rule in _RULES:
            match = rule.pattern.search(turn.content)
            if match is None:
                continue
            content = match.group("content").strip()
            if not content:
                continue

            key = (rule.category, content)
            draft = drafts.get(key)
            if draft is None:
                draft = _Draft(
                    category=rule.category,
                    content=content,
                    rule_id=rule.rule_id,
                )
                drafts[key] = draft
            if ref not in draft.refs:
                draft.refs.append(ref)
            draft.roleplay = draft.roleplay or roleplay
            draft.explicit = draft.explicit or explicit
            draft.hedged = draft.hedged or hedged
            draft.specific = draft.specific or has_specific_detail(content)

    def _build_candidate(self, candidate_id: str, draft: _Draft) -> MemoryCandidate:
        score, band, disposition = memory_confidence(
            user_evidence_count=len(draft.refs),
            explicit=draft.explicit,
            hedged=draft.hedged,
            specific=draft.specific,
            roleplay=draft.roleplay,
            sensitive=draft.category in _SENSITIVE_CATEGORIES,
        )
        flags: list[str] = []
        if draft.roleplay:
            flags.append("roleplay")
        if draft.category in _SENSITIVE_CATEGORIES:
            flags.append("sensitive")
        return MemoryCandidate(
            id=candidate_id,
            category=draft.category,
            content=draft.content,
            confidence=score,
            band=band,
            evidence=tuple(draft.refs),
            rule=draft.rule_id,
            disposition=disposition,
            flags=tuple(flags),
        )


__all__ = ["MemoryExtractor"]
