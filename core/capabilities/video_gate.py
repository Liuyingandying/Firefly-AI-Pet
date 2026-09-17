"""Video Analysis capability gate.

The Quick Tools「Video Analysis」plugin (``firefly-video``) is the user-facing
enable/disable switch. Every production path that reads video *content* —
Bilibili URLs, local video files, VideoStudy, transcript fetch, FFmpeg frame
extraction, video OCR — must consult this gate BEFORE touching video data.

The gate is a thin read-only adapter over the existing
``PluginLoader.is_plugin_enabled`` — the single enabled truth source. It never
caches the state (hot toggling takes effect on the next request) and never
introduces a second configuration.
"""

from __future__ import annotations

from typing import Any, Callable

PLUGIN_ID_VIDEO_ANALYSIS = "firefly-video"

_DISABLED_MESSAGE = (
    "Video Analysis 当前已关闭。可以在 Quick Tools 中开启后再让我分析这个视频。"
)


class VideoAnalysisDisabledError(RuntimeError):
    """Raised before any video data is read when the capability is off."""


def is_video_analysis_enabled(plugin_is_enabled: Callable[[str], bool] | None) -> bool:
    """Live read of the ``firefly-video`` plugin state (never cached).

    ``plugin_is_enabled`` is ``PluginLoader.is_plugin_enabled`` (or any
    compatible callable). A missing/broken source fails CLOSED — video reading
    must not silently proceed when the truth source cannot be verified.
    """
    if not callable(plugin_is_enabled):
        return False
    try:
        return bool(plugin_is_enabled(PLUGIN_ID_VIDEO_ANALYSIS))
    except Exception:  # noqa: BLE001 - a broken source must not allow reading
        return False


def require_video_analysis_enabled(
    plugin_is_enabled: Callable[[str], bool] | None,
) -> None:
    """Raise :class:`VideoAnalysisDisabledError` when the capability is off."""
    if not is_video_analysis_enabled(plugin_is_enabled):
        raise VideoAnalysisDisabledError()


def video_analysis_disabled_message() -> str:
    """User-facing reply when a video-reading request is refused.

    Deliberately exposes no plugin id, exception type or stack.
    """
    return _DISABLED_MESSAGE


__all__ = [
    "PLUGIN_ID_VIDEO_ANALYSIS",
    "VideoAnalysisDisabledError",
    "is_video_analysis_enabled",
    "require_video_analysis_enabled",
    "video_analysis_disabled_message",
]
