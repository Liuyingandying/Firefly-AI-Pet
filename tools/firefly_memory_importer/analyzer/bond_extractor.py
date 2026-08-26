"""Deterministic bond-signal extraction (Stage 1.4).

Pure Python: no ``core``/``memory``/``character`` imports and no LLM. Extracts
``BondCandidate`` signals from user turns only; assistant turns are never
relationship-fact sources. The extractor never writes bond state — it only
produces the signal candidates a later Stage 2 replays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Conversation, ParsedHistory, Role, Turn
from .attribution import has_ooc_marker, is_roleplay_heavy
from .candidates import BondCandidate, BondSignalType, SourceRef
from .confidence import bond_confidence


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    signal_type: BondSignalType
    pattern: re.Pattern[str]


_RULES = (
    _Rule("bond.thanked", BondSignalType.THANKED, re.compile(r"(?:谢谢|感谢|多谢)")),
    _Rule(
        "bond.shared_milestone",
        BondSignalType.SHARED_MILESTONE,
        re.compile(
            r"(?:我们一起|我们共同|我们)\s*(?:完成|达成|发布|写完|搞定|做成)(?:了)?"
            r"\s*(?P<detail>.+?)(?:[。！!？?]|$)"
        ),
    ),
    _Rule(
        "bond.promise_made",
        BondSignalType.PROMISE_MADE,
        re.compile(
            r"(?:下次|明天|以后|回头)\s*(?:再|继续|接着|还)\s*(?P<detail>.+?)(?:[。！!？?]|$)"
        ),
    ),
    _Rule(
        "bond.promise_kept",
        BondSignalType.PROMISE_KEPT,
        re.compile(r"(?:做完了|兑现了|做到了|履行了)\s*(?P<detail>.+?)(?:[。！!？?]|$)"),
    ),
    _Rule(
        "bond.promise_missed",
        BondSignalType.PROMISE_MISSED,
        re.compile(r"(?:没做完|没完成|没做到|忘了|没兑现)\s*(?P<detail>.+?)(?:[。！!？?]|$)"),
    ),
)


@dataclass
class _Draft:
    rule_id: str
    signal_type: BondSignalType
    detail: str | None
    refs: list[SourceRef] = field(default_factory=list)
    roleplay: bool = False


class BondExtractor:
    """Extract ``BondCandidate`` signals from user turns of a ``ParsedHistory``."""

    def extract(self, history: ParsedHistory) -> list[BondCandidate]:
        drafts: list[_Draft] = []
        detail_index: dict[tuple[BondSignalType, str], int] = {}

        for conversation in history.conversations:
            roleplay_heavy = is_roleplay_heavy(conversation)
            for turn in conversation.turns:
                if turn.role is not Role.USER:
                    continue
                ref = SourceRef(
                    conversation_id=conversation.id,
                    turn_seq=turn.seq,
                    role=turn.role,
                    ts=turn.ts,
                    message_id=turn.message_id,
                )
                roleplay = roleplay_heavy and not has_ooc_marker(turn.content)

                # TURN_COMPLETED: one mechanical signal per user turn.
                drafts.append(
                    _Draft("bond.turn_completed", BondSignalType.TURN_COMPLETED, None, [ref], roleplay)
                )

                for rule in _RULES:
                    match = rule.pattern.search(turn.content)
                    if match is None:
                        continue
                    detail = match.groupdict().get("detail")
                    if detail is not None:
                        detail = detail.strip()
                        if not detail:
                            continue

                    if detail is None:
                        # THANKED has no detail and is never deduplicated.
                        drafts.append(_Draft(rule.rule_id, rule.signal_type, None, [ref], roleplay))
                    else:
                        key = (rule.signal_type, detail)
                        index = detail_index.get(key)
                        if index is None:
                            index = len(drafts)
                            detail_index[key] = index
                            drafts.append(_Draft(rule.rule_id, rule.signal_type, detail, [ref], roleplay))
                        else:
                            draft = drafts[index]
                            if ref not in draft.refs:
                                draft.refs.append(ref)
                            draft.roleplay = draft.roleplay or roleplay

        drafts.sort(key=_sort_key)
        return [
            self._build_candidate(f"b-{index:04d}", draft)
            for index, draft in enumerate(drafts, start=1)
        ]

    def _build_candidate(self, candidate_id: str, draft: _Draft) -> BondCandidate:
        score, band, disposition = bond_confidence(
            draft.signal_type,
            label_source="user",
            strong=True,
            roleplay=draft.roleplay,
        )
        return BondCandidate(
            id=candidate_id,
            signal_type=draft.signal_type,
            confidence=score,
            band=band,
            evidence=tuple(draft.refs),
            rule=draft.rule_id,
            disposition=disposition,
            detail=draft.detail,
            flags=("roleplay",) if draft.roleplay else (),
        )


def _sort_key(draft: _Draft) -> tuple[int, str, str]:
    min_seq = min(ref.turn_seq for ref in draft.refs)
    return (min_seq, draft.signal_type.value, draft.detail or "")


__all__ = ["BondExtractor"]
