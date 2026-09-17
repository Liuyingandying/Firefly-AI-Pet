"""Composition root for one Firefly companion conversation turn.

This is the only module that wires Character, MemoryService,
ConversationStore, and BondStateEngine together. The domain modules remain
independent and the runtime only reads memory and bond state during a turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import Any, Mapping, Protocol, Sequence

from character.character_loader import CharacterLoader, CharacterProfile
from core.bond_state import BondState, BondStateEngine
from core.companion_config import CompanionConfig, load_companion_config
from core.companion_context_builder import CompanionContextBuilder, NarrativeReader
from core.conversation_store import ConversationStore
from memory.memory_manager import MemoryManager
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService
from memory.suggestion.suggestion_service import SuggestionService
from providers.base import ChatCompletion


logger = logging.getLogger(__name__)
_MISSING = object()


class ConversationProvider(Protocol):
    """Provider routing contract consumed by the companion runtime."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
    ) -> ChatCompletion: ...


class MemoryReader(Protocol):
    """Read-only part of MemoryService used during a turn."""

    def search(self, query: str, *, limit: int = 5) -> list[Any]: ...


class TurnStage(str, Enum):
    """Recoverable companion turn stages exposed for diagnostics."""

    BOND_READ = "bond_read"
    MEMORY_RETRIEVAL = "memory_retrieval"
    MEMORY_WRITE = "memory_write"
    CONVERSATION_LOAD = "conversation_load"
    CONVERSATION_SAVE = "conversation_save"
    NARRATIVE_READ = "narrative_read"


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Result of a companion turn including suggestion metadata."""

    response: Any
    suggestion_status: str = "none"
    suggestions_created: int = 0


@dataclass(frozen=True, slots=True)
class TurnError:
    """A recoverable module failure isolated from the provider response."""

    stage: TurnStage
    exception_type: str
    message: str


class _LegacyMemoryReader:
    """Deprecated read-only compatibility view over a v0.2 MemoryManager.

    M1 (Firefly_Global_Memory_Audit P0-1): production wiring no longer
    substitutes this reader for the real memory service. Kept only for
    out-of-tree callers that need a narrow read view — never assign it to
    :attr:`CompanionRuntime.memory_service`, which must be a full
    :class:`MemoryService` (remember/delete/clear/export included).
    """

    def __init__(self, manager: MemoryManager) -> None:
        self.manager = manager

    def search(self, query: str, *, limit: int = 5) -> list[Any]:
        return self.manager.search_memory(query)[:limit]


def _suggestion_store():
    """M3B.7: production persistent suggestion store in user data.

    Tests redirect the default path via conftest.
    """
    from memory.suggestion_store import SuggestionStore

    return SuggestionStore()


class CompanionRuntime:
    """Own dependency wiring and the lifecycle of one companion turn.

    Recoverable read/persistence failures are recorded in
    :attr:`last_turn_errors` and isolated. Character construction and provider
    execution remain fail-fast because a safe, genuine assistant response
    cannot be produced without them.
    """

    def __init__(
        self,
        character: CharacterProfile | None = None,
        memory_service: MemoryReader | None = None,
        conversation_store: ConversationStore | None | object = _MISSING,
        bond_state_engine: BondStateEngine | None | object = _MISSING,
        provider_router: ConversationProvider | None = None,
        narrative_reader: NarrativeReader | None = None,
        config: CompanionConfig | None = None,
        suggestion_service: SuggestionService | None = None,
    ) -> None:
        if config is None:
            config = load_companion_config()
        self.config = config
        if character is None:
            character = CharacterLoader().load()
        if memory_service is None:
            memory_service = MemoryService.local(
                JsonMemoryRepository(),
                write_policy=config.memory.write_policy,
                search_top_k=config.memory.search_top_k,
                search_threshold=config.memory.search_threshold,
            )
        if conversation_store is _MISSING:
            conversation_store = ConversationStore(
                max_messages=config.conversation.max_messages
            )
        if bond_state_engine is _MISSING:
            bond_state_engine = BondStateEngine()
        if provider_router is None:
            from core.ai_router import ProviderRouter

            provider_router = ProviderRouter()

        if not callable(getattr(character, "to_system_messages", None)):
            raise TypeError("character must provide to_system_messages()")
        if not callable(getattr(memory_service, "search", None)):
            raise TypeError("memory_service must provide search(query)")
        if conversation_store is not None:
            if not callable(getattr(conversation_store, "load_working_window", None)):
                raise TypeError("conversation_store must provide load_working_window()")
            if not callable(getattr(conversation_store, "append_exchange", None)):
                raise TypeError("conversation_store must provide append_exchange()")
        if bond_state_engine is not None and not callable(
            getattr(bond_state_engine, "read", None)
        ):
            raise TypeError("bond_state_engine must provide read()")
        if not callable(getattr(provider_router, "chat", None)):
            raise TypeError("provider_router must provide chat(messages)")

        self.character = character
        self.memory_service = memory_service
        self.conversation_store: ConversationStore | None = conversation_store
        self.bond_state_engine: BondStateEngine | None = bond_state_engine
        self.provider_router = provider_router
        self.context_builder = CompanionContextBuilder(
            character=character,
            memory_reader=memory_service,
            bond_reader=bond_state_engine,
            conversation_reader=conversation_store,
            narrative_reader=narrative_reader,
            on_error=self._on_context_error,
        )
        self.last_bond_state: BondState | None = None
        self.last_turn_errors: tuple[TurnError, ...] = ()
        self._turn_errors: list[TurnError] = []

        # P5A-1: suggestion service for auto memory formation
        if suggestion_service is not None:
            self.suggestion_service: SuggestionService | None = suggestion_service
        elif config.suggestion.enabled:
            extractor = None
            if config.suggestion.auto_extract_enabled and provider_router is not None:
                from memory.suggestion.candidate_extractor import MemoryCandidateExtractor

                extractor = MemoryCandidateExtractor(
                    provider_router,
                    max_candidates=config.suggestion.max_candidates_per_turn,
                )
            self.suggestion_service = SuggestionService(
                memory_service,
                extractor=extractor,
                enabled=config.suggestion.enabled,
                auto_extract_enabled=config.suggestion.auto_extract_enabled,
                max_candidates_per_turn=config.suggestion.max_candidates_per_turn,
                semantic_dedup_enabled=config.suggestion.semantic_dedup_enabled,
                semantic_dedup_threshold=config.suggestion.semantic_dedup_threshold,
                explicit_auto_approve_enabled=config.suggestion.explicit_auto_approve_enabled,
                explicit_auto_approve_min_confidence=config.suggestion.explicit_auto_approve_min_confidence,
                explicit_auto_approve_allowed_categories=config.suggestion.explicit_auto_approve_allowed_categories,
                auto_write_enabled=config.suggestion.auto_write_enabled,
                auto_write_min_chars=config.suggestion.auto_write_min_chars,
                auto_write_allowed_categories=config.suggestion.auto_write_allowed_categories,
                store=_suggestion_store(),
            )
        else:
            self.suggestion_service = None

    @classmethod
    def create_default(cls) -> CompanionRuntime:
        """Create the production companion graph in its sole composition root."""
        return cls()

    @classmethod
    def from_legacy(
        cls,
        memory_manager: MemoryManager | None = None,
        provider_router: ConversationProvider | None = None,
        *,
        character_loader: CharacterLoader | None = None,
        conversation_store: ConversationStore | None = None,
        _bond_path: str | None = None,
        config: CompanionConfig | None = None,
    ) -> CompanionRuntime:
        """Build the graph for the ConversationRuntime compatibility facade.

        M1 (Firefly_Global_Memory_Audit P0-1): the runtime now holds the FULL
        :class:`MemoryService` owned by the manager — remember / delete /
        clear / export and the panel all work. Reads go through the same
        service's ``search()`` (previously a ``_LegacyMemoryReader`` stripped
        every write capability).
        """
        manager = memory_manager if memory_manager is not None else MemoryManager()
        loader = character_loader if character_loader is not None else CharacterLoader()
        if provider_router is None:
            from core.ai_router import ProviderRouter

            provider_router = ProviderRouter()
        return cls(
            character=loader.load(),
            memory_service=manager.service,
            conversation_store=conversation_store,
            bond_state_engine=BondStateEngine(_bond_path),
            provider_router=provider_router,
            config=config,
        )

    def build_messages(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run the read half of a turn and build provider messages."""
        self._begin_turn()
        try:
            return self._build_messages(user_message, history=history)
        finally:
            self._finish_turn()

    def chat(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        turn_context: str | None = None,
    ) -> ChatCompletion:
        """Run one ordered turn: reads, provider call, then durable save.

        ``turn_context`` injects extra context (e.g. Screen Vision) into this
        turn's provider messages only: it is never persisted to the
        conversation store nor fed into post-turn memory suggestion
        extraction.
        """
        self._begin_turn()
        try:
            user_text = _required_text(user_message, "user_message")
            messages = self._build_messages(user_text, history=history)
            if turn_context:
                messages.insert(-1, {"role": "system", "content": turn_context})
            # M1: explicit "记住…" requests persist through the authoritative
            # MemoryService immediately (repository first, then semantic
            # index). Ordinary chat text is rejected by the write policy, so
            # this is a no-op unless the user explicitly asked to remember.
            self._try_explicit_remember(user_text)
            response = self.provider_router.chat(
                messages,
                model=model,
                temperature=temperature,
            )
            assistant_text = _assistant_content(response)
            if self.conversation_store is not None and assistant_text is not None:
                try:
                    self.conversation_store.append_exchange(user_text, assistant_text)
                except Exception as exc:
                    self._record_error(TurnStage.CONVERSATION_SAVE, exc)

            # P7: Advance bond state after a successful turn
            self._try_advance_bond(user_text)

            # P5A-1: Post-reply memory candidate extraction (non-blocking)
            self._try_extract_suggestions(user_text, assistant_text or "")

            return response
        finally:
            self._finish_turn()

    def _build_messages(
        self,
        user_message: str,
        *,
        history: Sequence[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        user_text = _required_text(user_message, "user_message")
        context = self.context_builder.build(user_text, history=history)
        self.last_bond_state = context.bond_state
        return context.to_messages(user_text)

    def _on_context_error(self, stage: str, exc: Exception) -> None:
        stages = {
            "bond_read": TurnStage.BOND_READ,
            "memory_retrieval": TurnStage.MEMORY_RETRIEVAL,
            "conversation_load": TurnStage.CONVERSATION_LOAD,
            "narrative_read": TurnStage.NARRATIVE_READ,
        }
        mapped = stages.get(stage)
        if mapped is not None:
            self._record_error(mapped, exc)

    def _begin_turn(self) -> None:
        self._turn_errors = []
        self.last_turn_errors = ()

    def _finish_turn(self) -> None:
        self.last_turn_errors = tuple(self._turn_errors)

    def _record_error(self, stage: TurnStage, exc: Exception) -> None:
        issue = TurnError(stage, type(exc).__name__, str(exc))
        self._turn_errors.append(issue)
        logger.warning(
            "Companion turn stage %s failed: %s",
            stage.value,
            exc,
            exc_info=True,
        )

    # ------------------------------------------------------------------ M1
    def _try_explicit_remember(self, user_message: str) -> None:
        """Persist an explicit "记住…" request through the full MemoryService.

        The service's write policy does the gating: under the default
        ``EXPLICIT_ONLY`` policy only messages matching the explicit-remember
        patterns produce a record — ordinary chat returns ``None`` without
        touching repository or index. Automatic extraction stays OFF (M1
        boundary); a read-only memory view simply skips the hook.
        """
        remember_detailed = getattr(self.memory_service, "remember_detailed", None)
        if not callable(remember_detailed):
            return  # read-only compatibility view: writes unsupported
        try:
            remember_detailed(user_message, trigger="explicit-command")
        except Exception as exc:
            # The turn continues; the failure is surfaced in diagnostics.
            self._record_error(TurnStage.MEMORY_WRITE, exc)

    # ------------------------------------------------------------------ P5A-1
    def _try_extract_suggestions(
        self, user_message: str, assistant_reply: str
    ) -> None:
        """Run post-reply candidate extraction, safely degraded on any failure."""
        if self.suggestion_service is None:
            return
        try:
            self.suggestion_service.extract_candidates(
                user_message,
                assistant_reply,
                context=list(self._get_recent_context()) if self.conversation_store else None,
            )
        except Exception as exc:
            # Extraction failure must never affect the user-visible response.
            logger.debug(
                "Post-reply suggestion extraction failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------ P7
    def _try_advance_bond(self, user_message: str) -> None:
        """Advance bond state after a successful companion turn."""
        if self.bond_state_engine is None:
            return
        try:
            from core.bond_rules import BondSignal, BondSignalType

            signal = BondSignal(BondSignalType.TURN_COMPLETED)
            self.bond_state_engine.apply(signal)
        except Exception as exc:
            logger.debug(
                "Bond state advance failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )

    def _get_recent_context(
        self,
    ) -> Sequence[dict[str, str]]:
        """Return recent conversation messages for extraction context."""
        if self.conversation_store is None:
            return []
        try:
            turns = self.conversation_store.load_working_window()
            result: list[dict[str, str]] = []
            for turn in turns[-6:]:  # last 3 turns
                if hasattr(turn, "to_chat_message"):
                    result.append(turn.to_chat_message())
                elif isinstance(turn, dict):
                    result.append(turn)
            return result
        except Exception:
            return []


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _assistant_content(response: Any) -> str | None:
    if not isinstance(response, Mapping):
        return None
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    message = choice.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content.strip()


__all__ = [
    "CompanionRuntime",
    "ConversationProvider",
    "MemoryReader",
    "TurnError",
    "TurnStage",
]
