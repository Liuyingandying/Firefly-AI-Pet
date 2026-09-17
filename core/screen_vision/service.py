"""ScreenVisionService facade: the single entry point Firefly calls.

    service.look(user_question, capture_mode=...) -> ScreenVisionResult

Capture modes (semantics v1):
- "primary_screen"        whole primary screen (alias: "primary")
- "last_non_firefly_window" the window the user was in before Firefly took
                           focus (default for unqualified look requests)
- "firefly_companion"      the Companion chat window itself
- "active_window"          legacy: current OS foreground
- "camera"                 Vision-1A: one camera frame ("看看我"), captured
                           by the dedicated CameraCapture grabber

Every look() performs exactly one on-demand capture. Nothing here runs or
captures in the background, writes to disk, or touches long-term memory.
"""

from time import perf_counter
from typing import Any

import os

from core.screen_vision.brain.base import ReasoningProvider
from core.screen_vision.models import ScreenObservation, ScreenVisionResult
from core.screen_vision.provider_errors import (
    EmptyProviderResponse,
    VisionTemporarilyUnavailable,
    classify_provider_exception,
    is_transient,
)
from core.screen_vision.screen.capture import ScreenCaptureService
from core.screen_vision.vision.base import VisionProvider

CAPTURE_MODES = (
    "primary_screen",
    "last_non_firefly_window",
    "firefly_companion",
    "primary",       # legacy alias of primary_screen
    "active_window", # legacy: current OS foreground
    "camera",        # Vision-1A: single camera frame ("看看我")
)

_CAPTURE_METHOD_BY_MODE = {
    "primary_screen": "capture_primary_screen",
    "primary": "capture_primary_screen",
    "last_non_firefly_window": "capture_last_non_firefly_window",
    "firefly_companion": "capture_firefly_companion",
    "active_window": "capture_active_window",
    "camera": "capture_camera",
}


class ScreenVisionService:
    def __init__(
        self,
        vision_provider: VisionProvider | None = None,
        reasoning_provider: ReasoningProvider | None = None,
        capture_service: ScreenCaptureService | None = None,
        settings: Any | None = None,
        fast_mode: bool | None = None,
        direct_vision_provider: Any | None = None,
        resilient_vision_provider: VisionProvider | None = None,
        resilient_reasoning_provider: ReasoningProvider | None = None,
        vision_breaker: Any | None = None,
    ):
        from core.screen_vision.config import normalize_routing_mode
        from core.screen_vision.circuit_breaker import CircuitBreaker

        self._settings = settings
        if fast_mode is None and settings is not None:
            fast_mode = bool(getattr(settings, "screen_vision_fast_mode", True))
        self._routing_mode = normalize_routing_mode(
            "fast" if fast_mode is not False else "resilient"
        )
        self._managed_providers = vision_provider is None or reasoning_provider is None
        if self._managed_providers:
            from core.screen_vision.config import (
                build_reasoning_provider,
                build_vision_provider,
            )

            vision_provider = vision_provider or build_vision_provider(self._routing_mode)
            reasoning_provider = reasoning_provider or build_reasoning_provider(self._routing_mode)
        self._vision = vision_provider
        self._reasoning = reasoning_provider
        self._capture = capture_service or ScreenCaptureService()
        self._last_capture_info: dict = {}
        candidate = getattr(self._vision, "_primary", self._vision)
        self._direct_vision = direct_vision_provider or (
            candidate if callable(getattr(candidate, "answer_direct", None)) else None
        )
        self._vision_breaker = (
            vision_breaker
            or getattr(self._vision, "_breaker", None)
            or CircuitBreaker()
        )
        self._resilient_vision = resilient_vision_provider
        self._resilient_reasoning = resilient_reasoning_provider
        if self._routing_mode == "resilient" or not self._managed_providers:
            self._resilient_vision = self._resilient_vision or self._vision
            self._resilient_reasoning = self._resilient_reasoning or self._reasoning

    @property
    def routing_mode(self) -> str:
        return self._routing_mode

    def set_routing_mode(self, mode: str) -> None:
        """Switch routing now (shared provider instances + circuit breakers)."""
        from core.screen_vision.config import (
            build_reasoning_provider,
            build_vision_provider,
            normalize_routing_mode,
        )

        mode = normalize_routing_mode(mode)
        if mode == self._routing_mode:
            return
        if self._managed_providers:
            self._vision = build_vision_provider(mode)
            self._reasoning = build_reasoning_provider(mode)
            if mode == "fast":
                candidate = getattr(self._vision, "_primary", self._vision)
                self._direct_vision = (
                    candidate
                    if callable(getattr(candidate, "answer_direct", None))
                    else None
                )
                self._vision_breaker = getattr(
                    self._vision, "_breaker", self._vision_breaker
                )
            else:
                self._resilient_vision = self._vision
                self._resilient_reasoning = self._reasoning
        self._routing_mode = mode

    def _capture_frame(self, capture_mode: str):
        """Produce one ScreenFrame for the requested capture mode.

        Screen modes go through the injected screen capture service. The
        camera source is standalone: when the injected service has no
        ``capture_camera`` (the production default), the dedicated
        CameraCapture grabber is used, so tests can inject a fake
        ``capture_camera`` instead and never touch a real camera.
        """
        method_name = _CAPTURE_METHOD_BY_MODE[capture_mode]
        if hasattr(self._capture, method_name):
            frame = getattr(self._capture, method_name)()
            self._last_capture_info = dict(
                getattr(self._capture, "last_capture_info", {})
            )
            return frame
        from core.screen_vision.screen.camera import CameraCapture

        camera = CameraCapture()
        frame = camera.capture_camera()
        self._last_capture_info = dict(camera.last_capture_info)
        return frame

    def _camera_trace(self, event: str, extra: str = "") -> None:
        """P0.2 diagnostic tracing (env-gated FIREFLY_CAMERA_TRACE=1)."""
        if not os.environ.get("FIREFLY_CAMERA_TRACE"):
            return
        try:
            from core.screen_vision.screen.camera import _trace

            _trace(event, extra)
        except Exception:  # noqa: BLE001
            pass

    def sync_routing_mode_from_settings(self) -> None:
        """Apply the Settings toggle immediately; no restart needed."""
        if self._settings is None:
            return
        fast = bool(getattr(self._settings, "screen_vision_fast_mode", True))
        self.set_routing_mode("fast" if fast else "resilient")

    def look(
        self,
        user_question: str,
        capture_mode: str = "last_non_firefly_window",
    ) -> ScreenVisionResult:
        """Capture once, observe, and answer. The only method that triggers
        a screenshot."""
        if capture_mode not in CAPTURE_MODES:
            raise ValueError(f"Unknown capture_mode {capture_mode!r}; expected one of {CAPTURE_MODES}.")

        self.sync_routing_mode_from_settings()
        total_started = perf_counter()

        capture_started = perf_counter()
        frame = self._capture_frame(capture_mode)
        capture_ms = (perf_counter() - capture_started) * 1000
        capture_info = dict(self._last_capture_info)
        if capture_mode == "camera":
            self._camera_trace("T9_look_got_frame", f"ms={capture_ms:.0f}")

        common = {
            "capture_mode": capture_mode,
            "capture_target": capture_info.get("capture_target", capture_mode),
            "capture_fallback_used": capture_info.get("capture_fallback_used", False),
        }
        if self._routing_mode == "fast" and self._direct_vision is not None:
            direct_started = perf_counter()
            try:
                if not self._vision_breaker.allow_primary():
                    raise VisionTemporarilyUnavailable("circuit_open")
                answer = self._direct_vision.answer_direct(frame, user_question)
                if not isinstance(answer, str) or not answer.strip():
                    raise EmptyProviderResponse()
                answer = answer.strip()
                direct_ms = (perf_counter() - direct_started) * 1000
                self._vision_breaker.record_success()
            except Exception as exc:  # noqa: BLE001 - classified below
                direct_ms = (perf_counter() - direct_started) * 1000
                if not is_transient(exc):
                    raise
                failure = classify_provider_exception(exc)
                if not isinstance(exc, VisionTemporarilyUnavailable):
                    self._vision_breaker.record_transient_failure(failure.failure_type)
                return self._look_resilient(
                    frame,
                    user_question,
                    capture_ms,
                    total_started,
                    common,
                    direct_ms=direct_ms,
                    direct_failure_type=failure.failure_type,
                )

            total_ms = (perf_counter() - total_started) * 1000
            timings = {
                "capture_ms": round(capture_ms, 1),
                "direct_vision_ms": round(direct_ms, 1),
                "total_ms": round(total_ms, 1),
            }
            return ScreenVisionResult(
                observation=ScreenObservation(),
                answer=answer,
                timings=timings,
                meta={
                    **common,
                    "routing_mode": "fast",
                    "remote_calls": 1,
                    "reasoning_calls": 0,
                    "fallback_used": False,
                    "direct_one_shot": True,
                    "vision_provider": getattr(
                        self._direct_vision, "name", "deepseek-vision"
                    ),
                    "vision_model": getattr(self._direct_vision, "model", "unknown"),
                    **timings,
                },
            )

        return self._look_resilient(
            frame, user_question, capture_ms, total_started, common
        )

    def _look_resilient(
        self,
        frame,
        user_question: str,
        capture_ms: float,
        total_started: float,
        common_meta: dict,
        *,
        direct_ms: float | None = None,
        direct_failure_type: str | None = None,
    ) -> ScreenVisionResult:
        """Run the frozen ScreenObservation -> text-only reasoning pipeline."""
        vision, reasoning = self._get_resilient_providers()
        vision_started = perf_counter()
        if direct_failure_type is None:
            observation = vision.inspect(frame)
        else:
            # The direct one-shot vision request (TJU-Qwen primary) already
            # failed this turn. Continue the resilient chain without
            # immediately retrying the DeepSeek vision fallback.
            observation = vision.inspect(
                frame, exclude_providers=("deepseek-vision",)
            )
        vision_ms = (perf_counter() - vision_started) * 1000

        payload = {
            key: value
            for key, value in observation.__dict__.items()
            if key != "raw_model_text"
        }
        reasoning_started = perf_counter()
        answer_text = reasoning.answer(user_question, payload)
        reasoning_ms = (perf_counter() - reasoning_started) * 1000
        total_ms = (perf_counter() - total_started) * 1000

        vision_meta = dict(getattr(vision, "last_meta", {}))
        reasoning_meta = dict(getattr(reasoning, "last_meta", {}))
        remote_calls = (
            (1 if direct_ms is not None else 0)
            + int(getattr(vision, "last_remote_calls", 1))
            + int(getattr(reasoning, "last_remote_calls", 1))
        )
        timings = {
            "capture_ms": round(capture_ms, 1),
            "vision_ms": round(vision_ms, 1),
            "reasoning_ms": round(reasoning_ms, 1),
            "total_ms": round(total_ms, 1),
        }
        if direct_ms is not None:
            timings["direct_vision_ms"] = round(direct_ms, 1)
        meta = {
            **common_meta,
            "routing_mode": self._routing_mode,
            "remote_calls": remote_calls,
            "reasoning_calls": int(getattr(reasoning, "last_remote_calls", 1)),
            "direct_one_shot": False,
            "fallback_used": direct_ms is not None,
            "vision_model": getattr(vision, "model", "unknown"),
            "reasoning_model": getattr(reasoning, "model", "unknown"),
            **vision_meta,
            **reasoning_meta,
            **timings,
        }
        if direct_failure_type is not None:
            meta["fallback_mode"] = "resilient"
            meta["direct_failure_type"] = direct_failure_type
        return ScreenVisionResult(
            observation=observation,
            answer=answer_text,
            timings=timings,
            meta=meta,
        )

    def _get_resilient_providers(self):
        if self._resilient_vision is None or self._resilient_reasoning is None:
            from core.screen_vision.config import (
                ROUTING_RESILIENT,
                build_reasoning_provider,
                build_vision_provider,
            )

            self._resilient_vision = self._resilient_vision or build_vision_provider(
                ROUTING_RESILIENT
            )
            self._resilient_reasoning = (
                self._resilient_reasoning
                or build_reasoning_provider(ROUTING_RESILIENT)
            )
        return self._resilient_vision, self._resilient_reasoning
