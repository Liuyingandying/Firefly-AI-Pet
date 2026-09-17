"""Phase v0.3-P10: Video Understanding pipeline.

Architecture:
  Video → FFmpeg → Audio + Frames
  Audio → ASR (faster-whisper / Whisper) → Subtitles
  Frames → scene detection → keyframes → OCR → vision model
  Final: Unified Timeline Context

  Supports:
  - Video summary
  - Segment summary
  - Subtitles (SRT)
  - OCR on keyframes
  - Query event timestamps
  - Explain formulas/charts in video frames
  - Key timestamps
  - Video QA

  Forbidden: sending all frames to LLM.
  Priority: subtitles + scene detection + keyframe + OCR.

Dependencies:
  - ffmpeg-python or subprocess ffmpeg
  - faster-whisper or openai-whisper (optional)
  - PySceneDetect (optional)
  - core.pdf_processor (for OCR on frames)
  - core.pdf_qa (for vision model QA on frames)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from core.pdf_processor import RapidOcrBackend

if TYPE_CHECKING:
    from core.video_vision import VideoVisionAdapter

log = logging.getLogger("firefly.p10_video")

# Hard cap on keyframes sent to the vision adapter per video, so a long video
# can never fan out into an unbounded number of remote vision calls.
MAX_VISION_KEYFRAMES = 8


# ======================================================================
# Data models
# ======================================================================


@dataclass
class VideoSegment:
    """A segment of video with start/end times."""

    start: float  # seconds
    end: float  # seconds
    summary: str | None = None
    key_timestamps: list[float] = field(default_factory=list)


@dataclass
class SubtitleEntry:
    """A single subtitle entry."""

    index: int
    start: float  # seconds
    end: float  # seconds
    text: str


@dataclass
class KeyframeInfo:
    """A detected keyframe from scene detection."""

    timestamp: float  # seconds
    image_bytes: bytes = b""
    ocr_text: str = ""
    description: str | None = None


@dataclass
class VideoTimeline:
    """Unified timeline context from video processing."""

    file_path: str
    duration: float = 0.0
    segments: list[VideoSegment] = field(default_factory=list)
    subtitles: list[SubtitleEntry] = field(default_factory=list)
    keyframes: list[KeyframeInfo] = field(default_factory=list)
    summary: str | None = None
    key_timestamps: list[float] = field(default_factory=list)
    stages: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    asr_backend: str | None = None
    scene_backend: str | None = None
    ocr_backend: str | None = None
    visual_description_supported: bool = False

    @property
    def full_transcript(self) -> str:
        return "\n".join(s.text for s in self.subtitles if s.text)


@dataclass
class VideoQaResult:
    """Result of a video QA operation."""

    answer: str = ""
    timestamps: list[float] = field(default_factory=list)
    related_segments: list[int] = field(default_factory=list)


# ======================================================================
# VideoProcessor
# ======================================================================


class VideoProcessor:
    """Video processing pipeline: FFmpeg extraction + scene detection + OCR.

    Uses FFmpeg for audio/video frame extraction.
    Uses PySceneDetect for scene detection.
    Uses faster-whisper for ASR.
    Uses core.pdf_processor for OCR on frames.
    """

    def __init__(
        self,
        ffmpeg_path: str | Path | None = None,
        *,
        asr_model: str = "tiny",
        vision_adapter: "VideoVisionAdapter | None" = None,
    ) -> None:
        if ffmpeg_path:
            self._ffmpeg = str(ffmpeg_path)
            # Validate it exists
            try:
                subprocess.run(
                    [self._ffmpeg, "-version"],
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
                self._ffmpeg = ""
        else:
            self._ffmpeg = self._find_ffmpeg()
        self._ffprobe = self._find_ffprobe()
        self._asr_model_name = asr_model
        self._whisper_model = None
        self._scenedetect_available = self._check_scenedetect()
        self._ocr_backend: RapidOcrBackend | None = None
        self._last_asr_backend: str | None = None
        self._vision_adapter = vision_adapter

    def _find_ffmpeg(self) -> str:
        """Find ffmpeg on PATH."""
        for name in ("ffmpeg", "ffmpeg.exe"):
            try:
                subprocess.run(
                    [name, "-version"],
                    capture_output=True,
                    check=True,
                    timeout=5,
                )
                return name
            except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
                continue
        return ""

    def _find_ffprobe(self) -> str:
        """Resolve ffprobe next to ffmpeg or from PATH."""
        if self._ffmpeg:
            ffmpeg_path = Path(self._ffmpeg)
            if ffmpeg_path.parent != Path("."):
                sibling = ffmpeg_path.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
                if sibling.is_file():
                    return str(sibling)
        return shutil.which("ffprobe") or shutil.which("ffprobe.exe") or ""

    @staticmethod
    def _check_scenedetect() -> bool:
        """Check if PySceneDetect is available."""
        try:
            import scenedetect  # noqa: F401
            return True
        except ImportError:
            return False

    def process(
        self,
        file_path: str | Path,
        *,
        extract_audio: bool = True,
        extract_frames: bool = True,
        scene_detection: bool = True,
        ocr_on_frames: bool = True,
        vision_on_frames: bool = False,
        video_analysis_enabled: Callable[[], bool] | None = None,
    ) -> VideoTimeline:
        """Process a video file through the full pipeline.

        Args:
            file_path: Path to the video file.
            extract_audio: Extract audio for ASR.
            extract_frames: Extract video frames.
            scene_detection: Run scene detection to find keyframes.
            ocr_on_frames: Run OCR on keyframe images.
            vision_on_frames: Describe keyframes through the injected vision
                adapter. Off by default: no vision model is called, no network
                I/O happens, and outputs are identical to the legacy pipeline.
            video_analysis_enabled: Optional capability gate (returns the live
                ``firefly-video`` plugin state). When provided and False, the
                pipeline raises :class:`VideoAnalysisDisabledError` BEFORE any
                ffprobe/ffmpeg/OCR work. None = legacy behavior (no gate).

        Returns:
            VideoTimeline with all extracted data.
        """
        if video_analysis_enabled is not None and not video_analysis_enabled():
            from core.capabilities.video_gate import VideoAnalysisDisabledError

            raise VideoAnalysisDisabledError()
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Video not found: {file_path}")

        timeline = VideoTimeline(file_path=str(file_path))

        timeline.stages["visual_description"] = "unsupported"

        # Get video duration
        timeline.duration = self._get_duration(file_path)
        timeline.stages["duration"] = "success" if timeline.duration > 0 else "error"
        if timeline.duration <= 0:
            timeline.errors.append("duration: ffprobe returned no positive duration")

        with tempfile.TemporaryDirectory(prefix="firefly-video-") as temp_dir:
            work_dir = Path(temp_dir)

            if extract_audio:
                audio_path = self._extract_audio(file_path, work_dir=work_dir)
                if audio_path:
                    timeline.stages["audio"] = "success"
                    try:
                        timeline.subtitles = self._transcribe_audio(audio_path)
                        timeline.asr_backend = self._last_asr_backend
                        timeline.stages["asr"] = (
                            "success" if timeline.subtitles else "empty"
                        )
                    except Exception as exc:
                        timeline.stages["asr"] = "error"
                        timeline.errors.append(f"asr: {type(exc).__name__}: {exc}")
                        log.warning("[VideoProcessor] ASR failed: %s", exc)
                else:
                    timeline.stages["audio"] = "error"
                    timeline.stages["asr"] = "blocked"
                    timeline.errors.append("audio: FFmpeg did not produce a WAV file")
            else:
                timeline.stages["audio"] = "skipped"
                timeline.stages["asr"] = "skipped"

            keyframes: list[KeyframeInfo] = []
            if scene_detection and self._scenedetect_available:
                try:
                    segments, keyframes = self._detect_scene_data(file_path, work_dir)
                    timeline.segments = segments
                    timeline.scene_backend = "PySceneDetect"
                    timeline.stages["scenes"] = "success" if segments else "empty"
                except Exception as exc:
                    timeline.stages["scenes"] = "error"
                    timeline.errors.append(f"scenes: {type(exc).__name__}: {exc}")
                    log.warning("[VideoProcessor] scene detection failed: %s", exc)
            elif scene_detection:
                timeline.stages["scenes"] = "blocked"
                timeline.errors.append("scenes: PySceneDetect is unavailable")
            else:
                timeline.stages["scenes"] = "skipped"

            if not keyframes and extract_frames:
                keyframes = self._extract_keyframes(
                    file_path,
                    interval=5.0,
                    work_dir=work_dir,
                )
            timeline.keyframes = keyframes
            timeline.key_timestamps = [frame.timestamp for frame in keyframes]
            timeline.stages["keyframes"] = "success" if keyframes else "empty"

            if ocr_on_frames and keyframes:
                self._ocr_backend = self._ocr_backend or RapidOcrBackend()
                timeline.ocr_backend = self._ocr_backend.label
                ocr_successes = 0
                for keyframe in keyframes:
                    try:
                        keyframe.ocr_text = self._ocr_frame(keyframe.image_bytes)
                        if keyframe.ocr_text:
                            ocr_successes += 1
                    except Exception as exc:
                        timeline.errors.append(
                            f"frame OCR at {keyframe.timestamp:.3f}s: "
                            f"{type(exc).__name__}: {exc}"
                        )
                timeline.stages["ocr"] = "success" if ocr_successes else "empty"
            else:
                timeline.stages["ocr"] = "skipped"

            self._run_vision_pass(timeline, keyframes, enabled=vision_on_frames)

        return timeline

    @staticmethod
    def _select_vision_frames(keyframes: list[KeyframeInfo]) -> list[KeyframeInfo]:
        """Pick at most MAX_VISION_KEYFRAMES frames, spread over the video."""
        total = len(keyframes)
        if total <= MAX_VISION_KEYFRAMES:
            return list(keyframes)
        step = total / MAX_VISION_KEYFRAMES
        return [keyframes[int(i * step)] for i in range(MAX_VISION_KEYFRAMES)]

    def _run_vision_pass(
        self,
        timeline: VideoTimeline,
        keyframes: list[KeyframeInfo],
        *,
        enabled: bool,
    ) -> None:
        """Optionally describe keyframes via the injected vision adapter.

        Serial, capped, and per-frame fault isolated: one failing frame is
        recorded in timeline.errors and never fails the video.
        """
        if not enabled:
            return  # legacy default: stage stays "unsupported", no provider call
        if self._vision_adapter is None:
            timeline.stages["visual_description"] = "blocked"
            timeline.errors.append("visual_description: no vision_adapter configured")
            return
        timeline.visual_description_supported = True
        if not keyframes:
            timeline.stages["visual_description"] = "empty"
            return
        described = 0
        for keyframe in self._select_vision_frames(keyframes):
            try:
                keyframe.description = self._vision_adapter.describe(
                    image_bytes=keyframe.image_bytes,
                    timestamp=keyframe.timestamp,
                    ocr_text=keyframe.ocr_text,
                )
                if keyframe.description:
                    described += 1
            except Exception as exc:
                timeline.errors.append(
                    f"frame vision at {keyframe.timestamp:.3f}s: "
                    f"{type(exc).__name__}: {exc}"
                )
                log.warning(
                    "[VideoProcessor] vision failed at %.3fs: %s",
                    keyframe.timestamp,
                    exc,
                )
        timeline.stages["visual_description"] = "success" if described else "empty"

    def _get_duration(self, file_path: Path) -> float:
        """Get video duration in seconds using ffprobe."""
        if not self._ffprobe:
            return 0.0
        try:
            result = subprocess.run(
                [
                    self._ffprobe,
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(file_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                duration = float(result.stdout.strip())
                return duration if duration > 0 else 0.0
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            return 0.0
        return 0.0

    @staticmethod
    def _parse_time(time_str: str) -> float:
        """Parse HH:MM:SS.ms to seconds."""
        parts = time_str.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return 0.0

    def _extract_audio(
        self,
        file_path: Path,
        *,
        work_dir: Path | None = None,
    ) -> str | None:
        """Extract audio from video as WAV. Returns temp file path."""
        if not self._ffmpeg:
            return None
        if work_dir is None:
            work_dir = Path(tempfile.mkdtemp(prefix="firefly-video-audio-"))
        work_dir.mkdir(parents=True, exist_ok=True)
        temp_path = str(work_dir / "audio.wav")
        try:
            result = subprocess.run(
                [
                    self._ffmpeg, "-y", "-i", str(file_path),
                    "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                    temp_path,
                ],
                capture_output=True,
                timeout=120,
            )
            if (
                result.returncode == 0
                and os.path.exists(temp_path)
                and os.path.getsize(temp_path) > 0
            ):
                return temp_path
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _transcribe_audio(self, audio_path: str) -> list[SubtitleEntry]:
        """Transcribe audio to subtitles using faster-whisper or whisper."""
        errors: list[str] = []
        try:
            from faster_whisper import WhisperModel

            if self._whisper_model is None:
                self._whisper_model = WhisperModel(
                    self._asr_model_name,
                    device="cpu",
                    compute_type="int8",
                )
            segments, _info = self._whisper_model.transcribe(
                audio_path,
                language=None,
            )
            entries = [
                SubtitleEntry(
                    index=index,
                    start=float(segment.start),
                    end=float(segment.end),
                    text=segment.text.strip(),
                )
                for index, segment in enumerate(segments, start=1)
                if segment.text.strip()
            ]
            self._last_asr_backend = f"faster-whisper:{self._asr_model_name}:cpu-int8"
            return entries
        except ImportError:
            errors.append("faster-whisper is not installed")
        except Exception as exc:
            errors.append(f"faster-whisper failed: {type(exc).__name__}: {exc}")

        try:
            import whisper

            model = whisper.load_model(self._asr_model_name)
            result = model.transcribe(audio_path)
            entries = [
                SubtitleEntry(
                    index=index,
                    start=float(segment["start"]),
                    end=float(segment["end"]),
                    text=str(segment["text"]).strip(),
                )
                for index, segment in enumerate(result.get("segments", []), start=1)
                if str(segment.get("text", "")).strip()
            ]
            self._last_asr_backend = f"openai-whisper:{self._asr_model_name}"
            return entries
        except ImportError:
            errors.append("openai-whisper is not installed")
        except Exception as exc:
            errors.append(f"openai-whisper failed: {type(exc).__name__}: {exc}")

        raise RuntimeError("; ".join(errors))

    def _detect_scenes(self, file_path: Path) -> list[KeyframeInfo]:
        """Detect scenes using PySceneDetect."""
        try:
            with tempfile.TemporaryDirectory(prefix="firefly-scenes-") as temp_dir:
                _segments, keyframes = self._detect_scene_data(
                    file_path,
                    Path(temp_dir),
                )
                return keyframes
        except Exception as exc:
            log.warning("[VideoProcessor] PySceneDetect failed: %s", exc)
            return []

    def _detect_scene_data(
        self,
        file_path: Path,
        work_dir: Path,
    ) -> tuple[list[VideoSegment], list[KeyframeInfo]]:
        """Return real scene ranges and midpoint keyframes via PySceneDetect."""
        from scenedetect import ContentDetector, detect

        scene_list = detect(
            str(file_path),
            ContentDetector(threshold=27.0),
            show_progress=False,
        )
        segments: list[VideoSegment] = []
        keyframes: list[KeyframeInfo] = []
        for start_time, end_time in scene_list:
            start_sec = float(start_time.get_seconds())
            end_sec = float(end_time.get_seconds())
            midpoint = (start_sec + end_sec) / 2.0
            keyframe = self._extract_frame_at(
                file_path,
                midpoint,
                work_dir=work_dir,
            )
            segments.append(
                VideoSegment(
                    start=start_sec,
                    end=end_sec,
                    key_timestamps=[midpoint] if keyframe else [],
                )
            )
            if keyframe:
                keyframes.append(keyframe)
        return segments, keyframes

    def _extract_keyframes(
        self,
        file_path: Path,
        interval: float = 5.0,
        *,
        work_dir: Path | None = None,
    ) -> list[KeyframeInfo]:
        """Extract frames at regular intervals as fallback."""
        keyframes: list[KeyframeInfo] = []
        duration = self._get_duration(file_path)

        t = 0.0
        while t < duration:
            kf = self._extract_frame_at(file_path, t, work_dir=work_dir)
            if kf:
                keyframes.append(kf)
            t += interval

        return keyframes

    def _extract_frame_at(
        self,
        file_path: Path,
        timestamp: float,
        *,
        work_dir: Path | None = None,
    ) -> KeyframeInfo | None:
        """Extract a single frame at a specific timestamp."""
        if not self._ffmpeg:
            return None

        if work_dir is None:
            work_dir = Path(tempfile.mkdtemp(prefix="firefly-video-frame-"))
        work_dir.mkdir(parents=True, exist_ok=True)
        temp_path = str(work_dir / f"frame-{int(timestamp * 1000):012d}.png")
        try:
            result = subprocess.run(
                [
                    self._ffmpeg, "-y", "-ss", str(timestamp),
                    "-i", str(file_path),
                    "-vframes", "1", "-q:v", "2",
                    temp_path,
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and os.path.exists(temp_path):
                img_bytes = Path(temp_path).read_bytes()
                os.unlink(temp_path)
                return KeyframeInfo(timestamp=timestamp, image_bytes=img_bytes)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _ocr_frame(self, image_bytes: bytes) -> str:
        """Run OCR on a frame image."""
        self._ocr_backend = self._ocr_backend or RapidOcrBackend()
        if not self._ocr_backend.available:
            raise RuntimeError(self._ocr_backend.error or "RapidOCR unavailable")
        return self._ocr_backend.extract_text(image_bytes)

    def generate_srt(self, subtitles: list[SubtitleEntry], file_path: str | Path) -> str:
        """Generate SRT subtitle file from subtitle entries."""
        lines = []
        for entry in subtitles:
            lines.append(str(entry.index))
            lines.append(self._format_srt_time(entry.start) + " --> " + self._format_srt_time(entry.end))
            lines.append(entry.text)
            lines.append("")

        srt_text = "\n".join(lines)
        srt_path = Path(file_path).with_suffix(".srt")
        try:
            srt_path.write_text(srt_text, encoding="utf-8")
        except OSError:
            pass
        return srt_text

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        """Format seconds to SRT timestamp HH:MM:SS,mmm."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ======================================================================
# VideoQA — question answering on video content
# ======================================================================


class VideoQa:
    """Video question answering using extracted timeline context."""

    def __init__(self, chat_handler=None) -> None:
        self._processor = VideoProcessor()
        self._chat = chat_handler

    def _get_chat(self):
        if self._chat is None:
            from core.ai_router import chat
            self._chat = chat
        return self._chat

    def answer(
        self,
        file_path: str | Path,
        question: str,
    ) -> VideoQaResult:
        """Answer a question about a video.

        Uses extracted subtitles + keyframe OCR text as context.
        """
        timeline = self._processor.process(file_path)

        # Build context from subtitles and OCR text
        context_parts = []
        if timeline.subtitles:
            context_parts.append("字幕:\n" + timeline.full_transcript[:5000])
        if timeline.keyframes:
            ocr_texts = [kf.ocr_text for kf in timeline.keyframes if kf.ocr_text]
            if ocr_texts:
                context_parts.append("关键帧OCR:\n" + "\n".join(ocr_texts)[:3000])

        if not context_parts:
            return VideoQaResult(answer="视频内容未提取到可分析的信息。")

        context = "\n\n".join(context_parts)
        prompt = (
            f"你是 Firefly，一个友好的视频助手。请根据以下视频提取的内容回答用户的问题。\n\n"
            f"视频内容：\n{context}\n\n"
            f"用户问题：{question}\n\n"
            f"如果答案不在内容中，请诚实地说不知道。"
        )

        chat = self._get_chat()
        messages = [{"role": "user", "content": prompt}]

        try:
            response = chat(messages, temperature=0.3)
            content = self._extract_content(response, default="")
            return VideoQaResult(answer=content)
        except Exception as exc:
            log.warning("[VideoQa] AI chat failed: %s", exc)
            return VideoQaResult(answer=f"无法回答：{exc}")

    def summarize(self, file_path: str | Path) -> VideoQaResult:
        """Generate a video summary."""
        timeline = self._processor.process(file_path)

        context = ""
        if timeline.subtitles:
            context = timeline.full_transcript[:8000]
        if not context:
            return VideoQaResult(answer="视频没有可提取的文本内容。")

        prompt = (
            "你是 Firefly，一个友好的视频助手。请根据以下视频字幕生成一份中文摘要。\n\n"
            f"视频字幕：\n{context}\n\n"
            "请提供：\n"
            "1. 视频的核心内容（1-2句）\n"
            "2. 主要要点（3-5个）\n"
            "3. 关键时间点"
        )

        chat = self._get_chat()
        messages = [{"role": "user", "content": prompt}]

        try:
            response = chat(messages, temperature=0.3)
            return VideoQaResult(answer=self._extract_content(response, default=""))
        except Exception as exc:
            log.warning("[VideoQa] AI chat failed for summary: %s", exc)
            return VideoQaResult(answer=f"无法生成摘要：{exc}")

    @staticmethod
    def _extract_content(response: dict, *, default: str = "") -> str:
        if not isinstance(response, dict):
            return default
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            return default
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            return default
        content = message.get("content", default)
        return content if isinstance(content, str) else default


__all__ = [
    "VideoProcessor",
    "VideoQa",
    "VideoTimeline",
    "VideoSegment",
    "SubtitleEntry",
    "KeyframeInfo",
    "VideoQaResult",
]
