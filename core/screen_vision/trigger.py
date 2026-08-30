"""Explicit screen-vision trigger rules.

Stage 1 policy: screen vision runs ONLY on an explicit user request.
The matcher prefers missing a request over false-triggering a screenshot
during ordinary chat.
"""

from core.screen_vision.models import ScreenVisionResult

DEFAULT_LOOK_QUESTION = "流萤，看一下我的屏幕，现在我在做什么？"
LOOK_COMMAND = "/look"

# Explicit "look at the screen/window" intents. Kept deliberately narrow.
_REQUEST_KEYWORDS = (
    "看一下屏幕", "看看屏幕", "看下屏幕", "看我的屏幕", "看一下我的屏幕",
    "看看我的屏幕", "看看当前窗口", "看一下当前窗口", "帮我看看当前窗口",
    "看看现在的屏幕", "看看这个窗口",
    "看看这是怎么回事", "你看看这是怎么回事", "看看这是怎么了",
    "看看我在做什么", "看看我在干什么", "看看我现在在做什么", "看看我现在在干什么",
    "看下我在做什么", "看下我在干什么",
    "look at my screen", "look at the screen", "what's on my screen",
)

# Generic combination rule: an explicit look-verb plus a screen/window target.
_LOOK_VERBS = ("看看", "看一下", "看下", "瞧瞧", "帮我看看", "你看看", "看看我的")
_SCREEN_TARGETS = ("屏幕", "当前窗口", "桌面", "显示器", "这是怎么回事",
                   "我在做什么", "我在干什么", "在做什么", "在干什么")

# Only these phrases upgrade the capture to the full primary screen.
_WHOLE_SCREEN_KEYWORDS = (
    "整个屏幕", "全屏", "整个桌面", "全部屏幕", "full screen", "entire screen",
)


def is_look_command(text: str) -> bool:
    """Deterministic debug entry: a bare ``/look`` (optionally with a question)."""
    stripped = (text or "").strip()
    return stripped == LOOK_COMMAND or stripped.startswith(LOOK_COMMAND + " ")


def is_explicit_screen_vision_request(text: str) -> bool:
    """True only for unambiguous 'look at my screen' requests."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if any(keyword in normalized for keyword in _REQUEST_KEYWORDS):
        return True
    has_look_verb = any(verb in normalized for verb in _LOOK_VERBS)
    has_screen_target = any(target in normalized for target in _SCREEN_TARGETS)
    return has_look_verb and has_screen_target


def resolve_capture_mode(text: str) -> str:
    """Default to the active window; full screen only when explicitly asked."""
    normalized = (text or "").strip().lower()
    if any(keyword in normalized for keyword in _WHOLE_SCREEN_KEYWORDS):
        return "primary"
    return "active_window"


def screen_vision_question(text: str) -> str:
    """Map the user input to the question used for the vision round."""
    stripped = (text or "").strip()
    if is_look_command(stripped):
        remainder = stripped[len(LOOK_COMMAND):].strip()
        return remainder or DEFAULT_LOOK_QUESTION
    return stripped


def format_screen_vision_context(result: ScreenVisionResult) -> str:
    """Render a ScreenVisionResult as the temporary current-turn context block.

    Text only: no image bytes, no base64, no credentials.
    """
    observation = result.observation
    lines = [
        "[Screen Vision Context]",
        "",
        "Qwen visual observation:",
        f"- 场景: {observation.scene_summary}",
        f"- 当前应用: {observation.active_application}",
        f"- 窗口标题: {observation.window_title}",
    ]
    if observation.visible_text:
        lines.append("- 可见文本: " + "; ".join(observation.visible_text[:15]))
    if observation.ui_elements:
        lines.append("- 界面元素: " + "; ".join(observation.ui_elements[:8]))
    if observation.warnings:
        lines.append("- 警告: " + "; ".join(observation.warnings))
    if observation.errors:
        lines.append("- 错误: " + "; ".join(observation.errors))
    if observation.uncertain:
        lines.append("- 视觉模块不确定项: " + "; ".join(observation.uncertain))
    lines += [
        "",
        "DeepSeek reasoning:",
        result.answer,
        "",
        f"Capture mode: {result.meta.get('capture_mode', 'active_window')}",
        "[End Screen Vision Context]",
    ]
    return "\n".join(lines)
