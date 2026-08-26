"""Deterministic style-preference extraction (Stage 1.3).

Pure Python: no ``core``/``memory``/``character`` imports and no LLM. Extracts
``StyleCandidate`` items from explicit user preferences only. Assistant turns
are never style sources (the old persona must not leak back). Every candidate
is ``review_only`` and never auto-applies to any character yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Conversation, ParsedHistory, Role, Turn
from .attribution import has_ooc_marker, is_roleplay_heavy
from .candidates import SourceRef, StyleCandidate, StyleDimension
from .confidence import style_confidence


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    dimension: StyleDimension
    pattern: re.Pattern[str]


_RULES = (
    _Rule(
        "style.nickname",
        StyleDimension.NICKNAME,
        re.compile(r"(?:叫我|称呼我|喊我)\s*(?P<content>[一-龥A-Za-z]{1,10})"),
    ),
    _Rule(
        "style.tone",
        StyleDimension.TONE,
        re.compile(
            r"(?:说话|语气)\s*(?P<content>(?:温柔|直接|轻松|撒娇|严肃|活泼|冷漠)(?:一点|些))"
        ),
    ),
    _Rule(
        "style.verbosity",
        StyleDimension.VERBOSITY,
        re.compile(
            r"(?:回复|回答|说话)\s*(?P<content>(?:短|长|简洁|简短|详细)(?:一点|些))"
        ),
    ),
    _Rule(
        "style.boundary",
        StyleDimension.BOUNDARY,
        re.compile(r"(?:不用|别)\s*每次都(?P<content>.+?)(?:[。！!？?]|$)"),
    ),
)


@dataclass
class _Draft:
    dimension: StyleDimension
    content: str
    rule_id: str
    refs: list[SourceRef] = field(default_factory=list)
    roleplay: bool = False


class StyleExtractor:
    """Extract ``StyleCandidate`` items from explicit user style preferences."""

    def extract(self, history: ParsedHistory) -> list[StyleCandidate]:
        drafts: dict[tuple[StyleDimension, str], _Draft] = {}
        for conversation in history.conversations:
            roleplay_heavy = is_roleplay_heavy(conversation)
            for turn in conversation.turns:
                if turn.role is not Role.USER:
                    continue
                self._collect(conversation, turn, roleplay_heavy, drafts)

        return [
            self._build_candidate(f"s-{index:04d}", draft)
            for index, draft in enumerate(drafts.values(), start=1)
        ]

    def _collect(
        self,
        conversation: Conversation,
        turn: Turn,
        roleplay_heavy: bool,
        drafts: dict[tuple[StyleDimension, str], _Draft],
    ) -> None:
        ref = SourceRef(
            conversation_id=conversation.id,
            turn_seq=turn.seq,
            role=turn.role,
            ts=turn.ts,
            message_id=turn.message_id,
        )
        roleplay = roleplay_heavy and not has_ooc_marker(turn.content)

        for rule in _RULES:
            match = rule.pattern.search(turn.content)
            if match is None:
                continue
            content = match.group("content").strip()
            if not content:
                continue

            key = (rule.dimension, content)
            draft = drafts.get(key)
            if draft is None:
                draft = _Draft(
                    dimension=rule.dimension,
                    content=content,
                    rule_id=rule.rule_id,
                )
                drafts[key] = draft
            if ref not in draft.refs:
                draft.refs.append(ref)
            draft.roleplay = draft.roleplay or roleplay

    def _build_candidate(self, candidate_id: str, draft: _Draft) -> StyleCandidate:
        score, band, disposition = style_confidence(
            explicit=True, roleplay=draft.roleplay
        )
        return StyleCandidate(
            id=candidate_id,
            dimension=draft.dimension,
            content=draft.content,
            confidence=score,
            band=band,
            evidence=tuple(draft.refs),
            rule=draft.rule_id,
            disposition=disposition,
            review_only=True,
            flags=("roleplay",) if draft.roleplay else (),
        )


__all__ = ["StyleExtractor"]
