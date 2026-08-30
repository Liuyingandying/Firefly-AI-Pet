"""Screen Vision: Qwen multimodal eyes + DeepSeek reasoning for Firefly.

Privacy invariants (enforced in code and tests):
- Screenshots happen only inside ScreenVisionService.look(), triggered by an
  explicit user request.
- Exactly one capture per look(); frames stay in memory and never touch disk.
- Only the VisionProvider sees image bytes; the ReasoningProvider payload is
  checked against image-data markers before every send.
- No background capture, no long-term memory writes, no secrets in logs.
"""

from core.screen_vision.models import ScreenFrame, ScreenObservation, ScreenVisionResult
from core.screen_vision.service import CAPTURE_MODES, ScreenVisionService
from core.screen_vision.trigger import (
    format_screen_vision_context,
    is_explicit_screen_vision_request,
    is_look_command,
    resolve_capture_mode,
    screen_vision_question,
)

__all__ = [
    "CAPTURE_MODES",
    "ScreenFrame",
    "ScreenObservation",
    "ScreenVisionResult",
    "ScreenVisionService",
    "format_screen_vision_context",
    "is_explicit_screen_vision_request",
    "is_look_command",
    "resolve_capture_mode",
    "screen_vision_question",
]
