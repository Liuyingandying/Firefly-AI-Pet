"""Firefly AI Pet UI V2 — companion console components.

New, additive UI surface: CharacterHeader + AbilityPanel + ChatView +
VideoCard composed by ``ui.v2.console.CompanionConsole``. All components are
pure rendering / signal shells: no core imports, no business logic, no state
ownership. The console wires them to the existing
``CharacterConversationRunner`` (read-only state consumption) and reuses
``ui.chat_markup`` / ``ui.theme`` so markdown rendering and the glass visual
language stay consistent.
"""

from ui.v2.ability_panel import AbilityPanel
from ui.v2.character_header import CharacterHeader
from ui.v2.chat_view import ChatView
from ui.v2.video_card import VideoCard, VideoCardInfo

__all__ = [
    "AbilityPanel",
    "CharacterHeader",
    "ChatView",
    "VideoCard",
    "VideoCardInfo",
]
