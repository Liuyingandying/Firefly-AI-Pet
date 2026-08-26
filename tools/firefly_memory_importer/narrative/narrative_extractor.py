"""NarrativeExtractor: extract companionship narrative from parsed history.

Phase 4-C. Unlike the fact-oriented ``MemoryExtractor``, this extracts
temporal, relationship-shaped narrative (life events, emotional turning points,
shared experiences, long-term goals) from user turns only. Assistant turns are
never fact sources; roleplay-heavy turns are down-weighted; every candidate
carries its evidence chain. It never writes Memory or Bond directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..analyzer.attribution import has_ooc_marker, is_roleplay_heavy
from ..analyzer.candidates import NarrativeCandidate, NarrativeType, SourceRef
from ..analyzer.confidence import (
    has_explicit_marker,
    has_hedging,
    has_specific_detail,
    memory_confidence,
)
from ..models import Conversation, ParsedHistory, Role, Turn


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    narrative_type: NarrativeType
    pattern: re.Pattern[str]


_RULES = (
    _Rule(
        "narr.life_event",
        NarrativeType.LIFE_EVENT,
        re.compile(
            r"我(?:已经|刚刚|正式)?(?P<content>(?:毕业|搬家|离职|入职|辞职|结婚|分手|考上|被录取|买房|生子|出国|回国|退休|创业|换工作|升职|转行)了)"
        ),
    ),
    _Rule(
        "narr.emotional_turning_point",
        NarrativeType.EMOTIONAL_TURNING_POINT,
        re.compile(
            r"我(?:决定|终于|突然)(?P<content>(?:重新开始|放下|释怀|想通|改变|走出来|看开|原谅)了)"
        ),
    ),
    _Rule(
        "narr.shared_experience",
        NarrativeType.SHARED_EXPERIENCE,
        re.compile(
            r"(?P<content>(?:我们一起|和你一起|我们)(?:度过|经历|熬过|走过|见证)(?:了)?[^。！!？?]*)"
        ),
    ),
    _Rule(
        "narr.long_term_goal",
        NarrativeType.LONG_TERM_GOAL,
        re.compile(
            r"我(?:想|希望|计划|打算)(?P<content>(?:三年|五年|十年|未来|以后|明年|将来|长期|一辈子|最终)[^。！!？?]*)"
        ),
    ),
)


@dataclass
class _Draft:
    narrative_type: NarrativeType
    content: str
    rule_id: str
    refs: list[SourceRef] = field(default_factory=list)
    roleplay: bool = False
    explicit: bool = False
    hedged: bool = False
    specific: bool = False


class NarrativeExtractor:
    """Extract ``NarrativeCandidate`` items from user turns of a ``ParsedHistory``."""

    def extract(self, history: ParsedHistory) -> list[NarrativeCandidate]:
        drafts: dict[tuple[NarrativeType, str], _Draft] = {}
        for conversation in history.conversations:
            roleplay_heavy = is_roleplay_heavy(conversation)
            for turn in conversation.turns:
                if turn.role is not Role.USER:
                    continue
                self._collect(conversation, turn, roleplay_heavy, drafts)

        return [
            self._build_candidate(f"n-{index:04d}", draft)
            for index, draft in enumerate(drafts.values(), start=1)
        ]

    def _collect(
        self,
        conversation: Conversation,
        turn: Turn,
        roleplay_heavy: bool,
        drafts: dict[tuple[NarrativeType, str], _Draft],
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

            key = (rule.narrative_type, content)
            draft = drafts.get(key)
            if draft is None:
                draft = _Draft(
                    narrative_type=rule.narrative_type,
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

    def _build_candidate(self, candidate_id: str, draft: _Draft) -> NarrativeCandidate:
        score, band, disposition = memory_confidence(
            user_evidence_count=len(draft.refs),
            explicit=draft.explicit,
            hedged=draft.hedged,
            specific=draft.specific,
            roleplay=draft.roleplay,
            sensitive=True,  # narrative is personal/emotional -> always needs review
        )
        flags: list[str] = []
        if draft.roleplay:
            flags.append("roleplay")
        flags.append("sensitive")
        return NarrativeCandidate(
            id=candidate_id,
            narrative_type=draft.narrative_type,
            content=draft.content,
            confidence=score,
            band=band,
            evidence=tuple(draft.refs),
            rule=draft.rule_id,
            disposition=disposition,
            flags=tuple(flags),
        )


__all__ = ["NarrativeExtractor"]
