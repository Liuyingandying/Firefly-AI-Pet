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
    "看看我电脑上是什么", "看看电脑上是什么",
    "看看这是怎么回事", "你看看这是怎么回事", "看看这是怎么了",
    "look at my screen", "look at the screen", "what's on my screen",
)

# Generic combination rule: an explicit look-verb plus a screen/window target.
_LOOK_VERBS = ("看看", "看一下", "看下", "瞧瞧", "帮我看看", "你看看", "看看我的")
_SCREEN_TARGETS = ("屏幕", "当前窗口", "桌面", "显示器", "这是怎么回事",
                   "电脑上是什么",
                   "聊天框", "聊天窗口", "你的窗口", "流萤窗口", "companion",
                   "对话框")

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
    """DEPRECATED legacy wrapper (primary / active_window names).

    Use :func:`resolve_capture_target` for the v1 capture semantics."""
    target = resolve_capture_target(text)
    return "primary" if target == CAPTURE_PRIMARY_SCREEN else "active_window"


# -------------------------------------------------- capture semantics v1

CAPTURE_PRIMARY_SCREEN = "primary_screen"
CAPTURE_LAST_NON_FIREFLY_WINDOW = "last_non_firefly_window"
CAPTURE_FIREFLY_COMPANION = "firefly_companion"
CAPTURE_CAMERA = "camera"
CAPTURE_TARGETS = (
    CAPTURE_PRIMARY_SCREEN,
    CAPTURE_LAST_NON_FIREFLY_WINDOW,
    CAPTURE_FIREFLY_COMPANION,
    CAPTURE_CAMERA,
)

# Vision-1A: explicit "look at me" (camera) intents. Checked only AFTER the
# screen/companion targets in resolve_capture_target, so "看看我的屏幕" keeps
# routing to the screen even though it starts with "看看我".
_CAMERA_TARGET_PHRASES = (
    "看看我", "看一下我", "看下我", "看我", "摄像头", "我的样子", "看到我吗",
)

# Priority 1: explicit whole screen / desktop.
_WHOLE_SCREEN_TARGET_PHRASES = (
    "整个屏幕", "全部屏幕", "整个桌面", "全部桌面", "全屏",
    "我的屏幕", "我的桌面",
)
# Priority 2: Firefly's own surfaces.
_COMPANION_TARGET_PHRASES = (
    "聊天框", "聊天窗口", "你的窗口", "流萤窗口", "流萤的窗口",
    "你的对话框", "这个对话框", "companion",
)
# Priority 3: "what I was just doing" surfaces.
_LAST_WINDOW_TARGET_PHRASES = (
    "刚才这个窗口", "刚才的窗口", "刚才在看", "在看的东西", "正在看的",
    "这个页面", "当前页面", "当前窗口",
    "这个窗口", "现在的画面",
)

_NEGATIONS = ("不用", "不要", "不用看", "别", "无需", "不需要", "不看", "没有看")


def _phrase_not_negated(text: str, index: int) -> bool:
    """A phrase match counts only when it is not directly negated, so
    "看看你这个聊天框，不用看整个桌面" is not misread as whole-screen."""
    prefix = text[max(0, index - 3):index]
    return not any(neg in prefix for neg in _NEGATIONS)


def _first_positive_phrase(text: str, phrases) -> int:
    best = -1
    for phrase in phrases:
        start = 0
        while True:
            index = text.find(phrase, start)
            if index == -1:
                break
            if _phrase_not_negated(text, index):
                return index
            start = index + len(phrase)
    return best


def is_camera_vision_request(text: str) -> bool:
    """True only for explicit "look at me" (camera) requests.

    Screen intents keep their existing routing: "看看我的屏幕" contains
    "看看我" but must never open the camera. Negation is respected the same
    way capture targets are ("不要用摄像头" is not a camera request).
    """
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if is_look_command(normalized) or is_explicit_screen_vision_request(normalized):
        return False
    return _first_positive_phrase(normalized, _CAMERA_TARGET_PHRASES) != -1


def resolve_capture_target(text: str) -> str:
    """Map an already-gated look request to WHERE to capture.

    Priority: explicit whole screen/desktop > Firefly's own window > the
    window the user was just in > default (last non-Firefly window).
    Callers must gate with is_explicit_screen_vision_request/is_look_command
    first; this function never decides WHETHER to capture.
    """
    normalized = (text or "").strip().lower()
    if not normalized:
        return CAPTURE_LAST_NON_FIREFLY_WINDOW
    if _first_positive_phrase(normalized, _WHOLE_SCREEN_TARGET_PHRASES) != -1:
        return CAPTURE_PRIMARY_SCREEN
    if _first_positive_phrase(normalized, _COMPANION_TARGET_PHRASES) != -1:
        return CAPTURE_FIREFLY_COMPANION
    if _first_positive_phrase(normalized, _LAST_WINDOW_TARGET_PHRASES) != -1:
        return CAPTURE_LAST_NON_FIREFLY_WINDOW
    if is_camera_vision_request(normalized):
        return CAPTURE_CAMERA
    return CAPTURE_LAST_NON_FIREFLY_WINDOW


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
        f"Capture target: {result.meta.get('capture_target', result.meta.get('capture_mode', 'last_non_firefly_window'))}",
        f"Capture fallback used: {result.meta.get('capture_fallback_used', False)}",
        "[End Screen Vision Context]",
    ]
    return "\n".join(lines)
