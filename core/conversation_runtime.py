"""Backward-compatible facade for :mod:`core.companion_runtime`."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Any, Sequence

from core.companion_runtime import CompanionRuntime, ConversationProvider
from providers.base import ChatCompletion

if TYPE_CHECKING:
    from character.character_loader import CharacterLoader
    from core.conversation_store import ConversationStore
    from memory.memory_manager import MemoryManager


class ConversationRuntime:
    """Compatibility API delegating all turn orchestration to CompanionRuntime."""

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        provider_router: ConversationProvider | None = None,
        *,
        character_loader: CharacterLoader | None = None,
        conversation_store: ConversationStore | None = None,
        companion_runtime: CompanionRuntime | None = None,
        _bond_path: str | None = None,
    ) -> None:
        self.memory_manager = memory_manager
        self.character_loader = character_loader
        self._runtime = companion_runtime or CompanionRuntime.from_legacy(
            memory_manager,
            provider_router,
            character_loader=character_loader,
            conversation_store=conversation_store,
            _bond_path=_bond_path,
        )

    @property
    def character(self):
        return self._runtime.character

    @property
    def conversation_store(self):
        return self._runtime.conversation_store

    @property
    def provider_router(self):
        return self._runtime.provider_router

    @property
    def last_bond_state(self):
        return self._runtime.last_bond_state

    @property
    def last_turn_errors(self):
        return self._runtime.last_turn_errors

    def build_messages(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        return self._runtime.build_messages(user_message, history=history)

    def chat(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> ChatCompletion:
        return self._runtime.chat(
            user_message,
            history=history,
            model=model,
            temperature=temperature,
        )


_default_runtime: ConversationRuntime | None = None
_default_runtime_lock = Lock()


def _get_default_runtime() -> ConversationRuntime:
    global _default_runtime
    if _default_runtime is None:
        with _default_runtime_lock:
            if _default_runtime is None:
                _default_runtime = ConversationRuntime()
    return _default_runtime


def chat(
    user_message: str,
    *,
    history: Sequence[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float = 0.2,
) -> ChatCompletion:
    """Chat through the default CompanionRuntime-backed facade."""
    return _get_default_runtime().chat(
        user_message,
        history=history,
        model=model,
        temperature=temperature,
    )


__all__ = ["ConversationRuntime", "chat"]
