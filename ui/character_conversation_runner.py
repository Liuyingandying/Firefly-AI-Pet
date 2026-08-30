"""Qt-safe bridge from the Firefly bubble to ConversationRuntime."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Signal

from core.agent_events import (
    STATUS_READING,
    STATUS_THINKING,
    AgentEvent,
    AgentEventType,
    ErrorCategory,
)
from core.conversation_runtime import ConversationRuntime
from core.conversation_store import ConversationStore
from core.screen_vision.trigger import (
    format_screen_vision_context,
    is_explicit_screen_vision_request,
    is_look_command,
    resolve_capture_mode,
    screen_vision_question,
)


logger = logging.getLogger(__name__)

SCREEN_VISION_FAILURE_REPLY = (
    "我这边的视觉模块这次没能看成屏幕（{reason}）。"
    "你可以稍后再试一次，或者直接把内容粘贴给我。"
)
SCREEN_VISION_UNAVAILABLE_REPLY = (
    "我这次暂时没能看清屏幕，视觉服务好像出了点问题。"
    "稍后再叫我看一次吧；也可以直接把内容粘贴给我。"
)
SCREEN_VISION_REASONING_UNAVAILABLE_REPLY = (
    "我已经看到了，但这次分析服务好像有些不稳定，稍后再问我一次吧。"
)


class CharacterConversationRunner(QObject):
    """Run non-streaming character turns without blocking the Qt UI thread."""

    AGENT_ID = "firefly"

    agent_event = Signal(object)

    def __init__(
        self,
        runtime: ConversationRuntime | None = None,
        parent: QObject | None = None,
        screen_vision_service: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self.runtime = runtime or ConversationRuntime(
            conversation_store=ConversationStore()
        )
        self._screen_vision_service = screen_vision_service
        self.last_screen_vision_timings: dict[str, float] | None = None
        self._busy = False
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._history = self._load_persisted_history()
        self._lock = threading.RLock()

    def _get_screen_vision_service(self) -> Any:
        """Lazily build the real service on first explicit request.

        Construction loads provider config from the environment; a missing
        configuration must not break Firefly startup, only the look request.
        """
        if self._screen_vision_service is None:
            from core.screen_vision.service import ScreenVisionService

            self._screen_vision_service = ScreenVisionService()
        return self._screen_vision_service

    def _load_persisted_history(self) -> list[dict[str, str]]:
        """Seed the UI history from ConversationStore when one is configured."""
        store = getattr(self.runtime, "conversation_store", None)
        loader = getattr(store, "load_working_window", None)
        if not callable(loader):
            return []
        try:
            return [turn.to_chat_message() for turn in loader()]
        except Exception:
            return []

    @property
    def running(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def has_history(self) -> bool:
        with self._lock:
            return bool(self._history)

    @property
    def history(self) -> list[dict[str, str]]:
        """Return a detached copy of the restored/in-process chat history."""
        with self._lock:
            return [dict(message) for message in self._history]



    def ask(self, prompt: str) -> bool:
        """Start a character turn and return False if the request is invalid."""
        text = (prompt or "").strip()
        with self._lock:
            if self._busy or not text:
                return False
            self._busy = True
            self._cancel_event = threading.Event()
            cancel_event = self._cancel_event

        self.agent_event.emit(
            AgentEvent.make(self.AGENT_ID, AgentEventType.STARTED)
        )
        self.agent_event.emit(
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.STATUS,
                status=STATUS_THINKING,
            )
        )
        thread = threading.Thread(
            target=self._run,
            args=(text, cancel_event),
            daemon=True,
            name="FireflyCharacterConversation",
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return True

    def stop(self) -> None:
        """Logically cancel an in-flight request and discard its late reply."""
        with self._lock:
            if self._cancel_event is not None:
                self._cancel_event.set()

    def clear_history(self) -> None:
        """Clear only the in-process conversation history, never long-term memory."""
        with self._lock:
            self._history.clear()

    def perform(
        self,
        prompt: str,
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[AgentEvent], str]:
        """Synchronously execute one turn for deterministic offline testing."""
        text = (prompt or "").strip()
        if not text:
            return [self._error_event("empty user message", ErrorCategory.PROTOCOL)], ""
        event = cancel_event or threading.Event()
        if event.is_set():
            return [self._cancelled_event()], ""
        with self._lock:
            history = [dict(message) for message in self._history]

        turn_context = None
        if is_look_command(text) or is_explicit_screen_vision_request(text):
            self.agent_event.emit(
                AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.STATUS,
                    status=STATUS_READING,
                )
            )
            try:
                result = self._get_screen_vision_service().look(
                    screen_vision_question(text),
                    capture_mode=resolve_capture_mode(text),
                )
            except Exception as exc:  # vision failure must not crash the turn
                from core.screen_vision.provider_errors import (
                    ReasoningTemporarilyUnavailable,
                    VisionTemporarilyUnavailable,
                )

                logger.warning(
                    "Screen vision look failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                if isinstance(exc, VisionTemporarilyUnavailable):
                    answer = SCREEN_VISION_UNAVAILABLE_REPLY
                elif isinstance(exc, ReasoningTemporarilyUnavailable):
                    answer = SCREEN_VISION_REASONING_UNAVAILABLE_REPLY
                else:
                    answer = SCREEN_VISION_FAILURE_REPLY.format(
                        reason=type(exc).__name__
                    )
                with self._lock:
                    self._history.extend(
                        [
                            {"role": "user", "content": text},
                            {"role": "assistant", "content": answer},
                        ]
                    )
                return [AgentEvent.make(
                    self.AGENT_ID,
                    AgentEventType.FINAL,
                    text=answer,
                )], answer
            turn_context = format_screen_vision_context(result)
            self._pending_vision_timings = dict(result.timings)
            self._pending_vision_meta = dict(result.meta)

        companion_started = time.perf_counter()
        try:
            response = self.runtime.chat(text, history=history, turn_context=turn_context)
            answer = extract_assistant_text(response)
        except Exception as exc:  # provider/runtime failures must not crash Qt
            return [self._error_event(str(exc), ErrorCategory.PROVIDER)], ""
        companion_ms = (time.perf_counter() - companion_started) * 1000
        if event.is_set():
            return [self._cancelled_event()], ""
        if turn_context is not None:
            vision_timings = getattr(self, "_pending_vision_timings", {}) or {}
            total_ms = vision_timings.get("total_ms", 0.0) + companion_ms
            self.last_screen_vision_timings = {
                **vision_timings,
                "companion_ms": round(companion_ms, 1),
                "total_ms": round(total_ms, 1),
            }
            logger.info(
                "Screen vision turn timings: %s",
                self.last_screen_vision_timings,
            )
        with self._lock:
            self._history.extend(
                [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": answer},
                ]
            )
        return [
            AgentEvent.make(
                self.AGENT_ID,
                AgentEventType.FINAL,
                text=answer,
            )
        ], answer

    def _run(self, prompt: str, cancel_event: threading.Event) -> None:
        events, _ = self.perform(prompt, cancel_event)
        with self._lock:
            self._busy = False
            self._cancel_event = None
            self._thread = None
        for event in events:
            self.agent_event.emit(event)

    def _error_event(self, message: str, category: ErrorCategory) -> AgentEvent:
        safe_message = (message or "character conversation failed").strip()
        return AgentEvent.make(
            self.AGENT_ID,
            AgentEventType.ERROR,
            text=safe_message,
            error_code=category,
        )

    def _cancelled_event(self) -> AgentEvent:
        return AgentEvent.make(
            self.AGENT_ID,
            AgentEventType.CANCELLED,
            error_code=ErrorCategory.CANCELLED,
        )


def extract_assistant_text(response: Any) -> str:
    """Extract one non-empty assistant reply from a chat completion."""
    if not isinstance(response, dict):
        raise ValueError("conversation response must be an object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("conversation response has no choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("conversation response has no assistant text")
    return content.strip()
