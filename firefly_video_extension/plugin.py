"""Firefly Video Extension Phase 2-A — minimal VideoPipeline capability plugin.

Deliberately scoped to what Extension API v2 + VideoPipeline already provide:

- ``initialize(context)``   -> keep the injected PluginContext; build a
                               ``VideoProcessor`` with NO vision adapter
                               (Phase 2-A never constructs a VisionProvider)
- ``start()``               -> publish plugin.status=READY
- ``open()``                -> Quick Tools activation: pick a local video,
                               run the standard pipeline with
                               ``vision_on_frames=False``, show a short
                               summary, publish READY
- ``stop()``                -> publish plugin.status=OFFLINE

Phase 2-A boundary: no Memory writes, no new RuntimeEvent kinds, no vision
provider, no camera, no modifications to core or UI.  The file dialog and the
result popup are thin seams (``_select_video`` / ``_present``) so the lifecycle
is testable without a live Qt dialog.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QMessageBox

from core.extension_api import (
    EXT_STATUS_ERROR,
    EXT_STATUS_OFFLINE,
    EXT_STATUS_READY,
    EXT_STATUS_WORKING,
    FireflyExtension,
    PluginContext,
)
from core.quick_tools import QuickToolManifest
from core.video_pipeline import VideoProcessor

log = logging.getLogger("firefly.quick_tools.video_extension")

PLUGIN_ID = "firefly-video"
PLUGIN_VERSION = "0.1.0"
_VIDEO_FILTER = "视频文件 (*.mp4 *.webm *.mov *.avi *.mkv);;所有文件 (*)"


class FireflyVideoExtension(FireflyExtension):
    capabilities = ("vision",)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._processor: VideoProcessor | None = None
        self.last_timeline = None

    # -- manifest -----------------------------------------------------------

    @property
    def manifest(self) -> QuickToolManifest:
        return QuickToolManifest(
            id=PLUGIN_ID,
            name="Video Analysis",
            description="分析本地视频：时长 / 场景 / 关键帧 / 字幕（Phase 2-A 不启用视觉模型）",
            icon="video",
            version=PLUGIN_VERSION,
            capabilities=self.capabilities,
            min_api="2",
            config_defaults={"vision_enabled": False},
            author="Firefly",
        )

    @property
    def capability(self) -> str:
        return "video"

    @property
    def capability_note(self) -> str:
        return "本地视频分析（FFmpeg + 场景检测 + OCR）"

    # -- lifecycle ----------------------------------------------------------

    def initialize(self, context: PluginContext) -> None:
        super().initialize(context)
        # Phase 2-A: no vision adapter is injected — vision stays off.
        self._processor = VideoProcessor()

    def start(self) -> None:
        self.publish_status(EXT_STATUS_READY, "video extension ready")

    def open(self) -> None:
        video_path = self._select_video()
        if not video_path:
            return  # user cancelled the picker; no analysis, no status change
        processor = self._processor
        if processor is None:
            return
        self.publish_status(EXT_STATUS_WORKING, f"分析中：{Path(video_path).name}")
        try:
            timeline = processor.process(video_path, vision_on_frames=False)
        except Exception as exc:  # processing must never crash the caller
            log.warning("video extension process failed: %s", type(exc).__name__)
            self.publish_status(EXT_STATUS_ERROR, "视频分析失败")
            self._present("视频分析失败", f"处理视频时出错：{type(exc).__name__}")
            return
        self.last_timeline = timeline
        self._present("视频分析完成", self._format_summary(timeline))
        self.publish_status(EXT_STATUS_READY, "video extension ready")

    def stop(self) -> None:
        self.publish_status(EXT_STATUS_OFFLINE, "video extension stopped")

    def shutdown(self) -> None:
        """Release owned resources. Phase 2-A holds no persistent resources."""
        self._processor = None

    # -- UI seams (overridable in tests) -------------------------------------

    def _select_video(self) -> str | None:
        """Open a native file picker; returns the chosen path or None."""
        path, _selected = QFileDialog.getOpenFileName(
            None, "选择要分析的本地视频", "", _VIDEO_FILTER
        )
        return path or None

    def _present(self, title: str, text: str) -> None:
        """Show the analysis outcome in a native message box."""
        QMessageBox.information(None, title, text)

    # -- summary --------------------------------------------------------------

    def _format_summary(self, timeline) -> str:
        duration = float(getattr(timeline, "duration", 0.0) or 0.0)
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        stages = dict(getattr(timeline, "stages", {}) or {})

        def stage(name: str) -> str:
            return str(stages.get(name, "unknown"))

        lines = [
            f"文件：{getattr(timeline, 'file_path', '?')}",
            f"时长：{minutes}:{seconds:02d}",
            "关键帧：{} ｜ 场景：{} ｜ 字幕条目：{}".format(
                len(getattr(timeline, "keyframes", []) or []),
                len(getattr(timeline, "segments", []) or []),
                len(getattr(timeline, "subtitles", []) or []),
            ),
            "场景检测：{} ｜ OCR：{} ｜ 视觉描述：{}".format(
                stage("scenes"),
                stage("ocr"),
                stage("visual_description"),
            ),
        ]
        errors = getattr(timeline, "errors", None) or []
        if errors:
            lines.append("部分环节未完成：{}".format("；".join(str(e) for e in errors[:3])))
        return "\n".join(lines)


def create_plugin(parent=None) -> FireflyVideoExtension:
    return FireflyVideoExtension(parent)
