"""Deterministic attribution rules for Firefly history migration Stage 1.

Pure functions with no ``core``/``memory``/``character`` imports and no LLM.
Implements the user/assistant attribution rule (R1) and the roleplay detection
used to penalize in-character user text.
"""

from __future__ import annotations

import re
from dataclasses import replace

from ..models import Conversation, Role
from .candidates import Band, Disposition, SourceRef


STAGE_DIRECTION_PATTERNS = (
    re.compile(r"（[^）]*）"),  # full-width parenthetical （…）
    re.compile(r"\([^)]*\)"),  # half-width parenthetical (…)
    re.compile(r"\*[^*]+\*"),  # asterisk action *…*
)

OOC_PATTERNS = (
    re.compile(r"\(\s*\("),  # ((
    re.compile(r"（\s*\("),  # （(
    re.compile(r"ooc\s*[:：]", re.IGNORECASE),
    re.compile(r"跳出角色"),
    re.compile(r"现实里"),
)


def has_stage_direction(text: str) -> bool:
    """Return whether ``text`` contains roleplay stage-direction markers."""
    return any(pattern.search(text) for pattern in STAGE_DIRECTION_PATTERNS)


def roleplay_score(conversation: Conversation) -> float:
    """Return the fraction of assistant turns carrying stage-direction markers."""
    assistant_turns = [turn for turn in conversation.turns if turn.role is Role.ASSISTANT]
    if not assistant_turns:
        return 0.0
    marked = sum(1 for turn in assistant_turns if has_stage_direction(turn.content))
    return marked / len(assistant_turns)


def is_roleplay_heavy(
    conversation: Conversation, *, threshold: float = 0.5
) -> bool:
    """Return whether ``conversation`` is roleplay-heavy at ``threshold``."""
    return roleplay_score(conversation) >= threshold


def has_ooc_marker(text: str) -> bool:
    """Return whether ``text`` signals out-of-character (user speaking as themselves)."""
    return any(pattern.search(text) for pattern in OOC_PATTERNS)


def has_user_evidence(evidence: tuple[SourceRef, ...]) -> bool:
    """Return whether at least one evidence reference is a user turn."""
    return any(ref.role is Role.USER for ref in evidence)


def enforce_user_evidence(candidate):
    """Apply R1: reject fact-bearing candidates with no user evidence.

    Returns ``candidate`` unchanged when it already has user evidence; otherwise
    returns a copy flagged ``assistant_derived``, demoted to ``rejected`` and
    ``auto_reject``. Intended for memory/style candidates.
    """
    if has_user_evidence(candidate.evidence):
        return candidate
    flags = _add_flag(candidate.flags, "assistant_derived")
    return replace(
        candidate,
        flags=flags,
        band=Band.REJECTED,
        disposition=Disposition.AUTO_REJECT,
    )


def _add_flag(flags: tuple[str, ...], flag: str) -> tuple[str, ...]:
    if flag in flags:
        return flags
    return (*flags, flag)


__all__ = [
    "enforce_user_evidence",
    "has_ooc_marker",
    "has_stage_direction",
    "has_user_evidence",
    "is_roleplay_heavy",
    "roleplay_score",
]
