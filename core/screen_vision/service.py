"""ScreenVisionService facade: the single entry point Firefly calls.

    service.look(user_question, capture_mode=...) -> ScreenVisionResult

Capture modes (semantics v1):
- "primary_screen"        whole primary screen (alias: "primary")
- "last_non_firefly_window" the window the user was in before Firefly took
                           focus (default for unqualified look requests)
- "firefly_companion"      the Companion chat window itself
- "active_window"          legacy: current OS foreground

Every look() performs exactly one on-demand capture. Nothing here runs or
captures in the background, writes to disk, or touches long-term memory.
"""

from time import perf_counter

from core.screen_vision.brain.base import ReasoningProvider
from core.screen_vision.models import ScreenVisionResult
from core.screen_vision.screen.capture import ScreenCaptureService
from core.screen_vision.vision.base import VisionProvider

CAPTURE_MODES = (
    "primary_screen",
    "last_non_firefly_window",
    "firefly_companion",
    "primary",       # legacy alias of primary_screen
    "active_window", # legacy: current foreground
)

_CAPTURE_METHOD_BY_MODE = {
    "primary_screen": "capture_primary_screen",
    "primary": "capture_primary_screen",
    "last_non_firefly_window": "capture_last_non_firefly_window",
    "firefly_companion": "capture_firefly_companion",
    "active_window": "capture_active_window",
}


class ScreenVisionService:
    def __init__(
        self,
        vision_provider: VisionProvider | None = None,
        reasoning_provider: ReasoningProvider | None = None,
        capture_service: ScreenCaptureService | None = None,
    ):
        if vision_provider is None or reasoning_provider is None:
            from core.screen_vision.config import (
                build_reasoning_provider,
                build_vision_provider,
            )

            vision_provider = vision_provider or build_vision_provider()
            reasoning_provider = reasoning_provider or build_reasoning_provider()
        self._vision = vision_provider
        self._reasoning = reasoning_provider
        self._capture = capture_service or ScreenCaptureService()

    def look(
        self,
        user_question: str,
        capture_mode: str = "last_non_firefly_window",
    ) -> ScreenVisionResult:
        """Capture once, observe, and answer. The only method that triggers
        a screenshot."""
        if capture_mode not in CAPTURE_MODES:
            raise ValueError(f"Unknown capture_mode {capture_mode!r}; expected one of {CAPTURE_MODES}.")

        total_started = perf_counter()

        capture_started = perf_counter()
        frame = getattr(self._capture, _CAPTURE_METHOD_BY_MODE[capture_mode])()
        capture_ms = (perf_counter() - capture_started) * 1000
        capture_info = dict(getattr(self._capture, "last_capture_info", {}))

        vision_started = perf_counter()
        observation = self._vision.inspect(frame)
        vision_ms = (perf_counter() - vision_started) * 1000

        payload = {
            key: value
            for key, value in observation.__dict__.items()
            if key != "raw_model_text"
        }
        reasoning_started = perf_counter()
        answer_text = self._reasoning.answer(user_question, payload)
        reasoning_ms = (perf_counter() - reasoning_started) * 1000

        total_ms = (perf_counter() - total_started) * 1000

        return ScreenVisionResult(
            observation=observation,
            answer=answer_text,
            timings={
                "capture_ms": round(capture_ms, 1),
                "vision_ms": round(vision_ms, 1),
                "reasoning_ms": round(reasoning_ms, 1),
                "total_ms": round(total_ms, 1),
            },
            meta={
                "capture_mode": capture_mode,
                "capture_target": capture_info.get("capture_target", capture_mode),
                "capture_fallback_used": capture_info.get("capture_fallback_used", False),
                "vision_model": getattr(self._vision, "model", "unknown"),
                "reasoning_model": getattr(self._reasoning, "model", "unknown"),
                **getattr(self._vision, "last_meta", {}),
                **getattr(self._reasoning, "last_meta", {}),
            },
        )
