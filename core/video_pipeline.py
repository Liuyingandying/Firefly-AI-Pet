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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger("firefly.p10_video")


# ======================================================================
# Data models
# ======================================================================


@dataclass
class VideoSegment:
    """A segment of video with start/end times."""

    start: float  # seconds
    end: float  # seconds
    summary: str = ""
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
    description: str = ""


@dataclass
class VideoTimeline:
    """Unified timeline context from video processing."""

    file_path: str
    duration: float = 0.0
    segments: list[VideoSegment] = field(default_factory=list)
    subtitles: list[SubtitleEntry] = field(default_factory=list)
    keyframes: list[KeyframeInfo] = field(default_factory=list)
    summary: str = ""
    key_timestamps: list[float] = field(default_factory=list)

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

    def __init__(self, ffmpeg_path: str | Path | None = None) -> None:
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
        self._whisper_model = None
        self._scenedetect_available = self._check_scenedetect()

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

    @staticmethod
    def _check_scenedetect() -> bool:
        """Check if PySceneDetect is available."""
        try:
            import scene_detect  # noqa: F401
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
        ocr_on_frames: bool = False,
    ) -> VideoTimeline:
        """Process a video file through the full pipeline.

        Args:
            file_path: Path to the video file.
            extract_audio: Extract audio for ASR.
            extract_frames: Extract video frames.
            scene_detection: Run scene detection to find keyframes.
            ocr_on_frames: Run OCR on keyframe images.

        Returns:
            VideoTimeline with all extracted data.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"Video not found: {file_path}")

        timeline = VideoTimeline(file_path=str(file_path))

        # Get video duration
        timeline.duration = self._get_duration(file_path)

        # Extract audio for ASR
        if extract_audio:
            audio_path = self._extract_audio(file_path)
            if audio_path:
                subtitles = self._transcribe_audio(audio_path)
                timeline.subtitles = subtitles
                os.unlink(audio_path)

        # Scene detection + keyframe extraction
        if scene_detection and self._scenedetect_available:
            keyframes = self._detect_scenes(file_path)
            if ocr_on_frames:
                for kf in keyframes:
                    kf.ocr_text = self._ocr_frame(kf.image_bytes)
            timeline.keyframes = keyframes
        elif extract_frames:
            # Fallback: extract frames at regular intervals
            keyframes = self._extract_keyframes(file_path, interval=5.0)
            if ocr_on_frames:
                for kf in keyframes:
                    kf.ocr_text = self._ocr_frame(kf.image_bytes)
            timeline.keyframes = keyframes

        return timeline

    def _get_duration(self, file_path: Path) -> float:
        """Get video duration in seconds using ffprobe."""
        if not self._ffmpeg:
            return 0.0
        try:
            result = subprocess.run(
                [
                    self._ffmpeg, "-i", str(file_path),
                    "-hide_banner", "-loglevel", "error",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # Parse Duration from stderr
            for line in result.stderr.split("\n"):
                if "Duration" in line:
                    # Format: Duration: 00:12:34.56, start: ...
                    parts = line.split(",")[0].strip()
                    if "Duration:" in parts:
                        time_str = parts.split("Duration:")[1].strip()
                        return self._parse_time(time_str)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
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

    def _extract_audio(self, file_path: Path) -> str | None:
        """Extract audio from video as WAV. Returns temp file path."""
        if not self._ffmpeg:
            return None
        temp_path = str(file_path.with_suffix(".wav"))
        try:
            subprocess.run(
                [
                    self._ffmpeg, "-y", "-i", str(file_path),
                    "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                    temp_path,
                ],
                capture_output=True,
                timeout=120,
            )
            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                return temp_path
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _transcribe_audio(self, audio_path: str) -> list[SubtitleEntry]:
        """Transcribe audio to subtitles using faster-whisper or whisper."""
        entries: list[SubtitleEntry] = []

        # Try faster-whisper first
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel("tiny", device="cpu", compute_type="int8")
            segments, info = model.transcribe(audio_path, language="zh" if info.language and "zh" in info.language else None)
            idx = 1
            for seg in segments:
                entries.append(SubtitleEntry(
                    index=idx,
                    start=seg.start,
                    end=seg.end,
                    text=seg.text.strip(),
                ))
                idx += 1
            return entries
        except ImportError:
            pass

        # Try openai-whisper
        try:
            import whisper
            model = whisper.load_model("tiny")
            result = model.transcribe(audio_path, language="zh")
            idx = 1
            for seg in result["segments"]:
                entries.append(SubtitleEntry(
                    index=idx,
                    start=seg["start"],
                    end=seg["end"],
                    text=seg["text"].strip(),
                ))
                idx += 1
            return entries
        except ImportError:
            pass

        log.warning("[VideoProcessor] No ASR engine available (faster-whisper or whisper)")
        return entries

    def _detect_scenes(self, file_path: Path) -> list[KeyframeInfo]:
        """Detect scenes using PySceneDetect."""
        keyframes: list[KeyframeInfo] = []

        try:
            from scene_detect import OpenCVDetector, detect_scenes
            from scene_detect.detectors import ContentDetector

            result = detect_scenes(
                str(file_path),
                ContentDetector(threshold=27.0),
                start_time=None,
                end_time=None,
            )

            for scene in result:
                start_sec = scene[0].get_seconds()
                end_sec = scene[1].get_seconds() if len(scene) > 1 else start_sec + 1.0
                # Extract keyframe at middle of scene
                mid = (start_sec + end_sec) / 2
                kf = self._extract_frame_at(file_path, mid)
                if kf:
                    keyframes.append(kf)

        except ImportError:
            log.warning("[VideoProcessor] PySceneDetect not available, using interval extraction")

        return keyframes

    def _extract_keyframes(self, file_path: Path, interval: float = 5.0) -> list[KeyframeInfo]:
        """Extract frames at regular intervals as fallback."""
        keyframes: list[KeyframeInfo] = []
        duration = self._get_duration(file_path)

        t = 0.0
        while t < duration:
            kf = self._extract_frame_at(file_path, t)
            if kf:
                keyframes.append(kf)
            t += interval

        return keyframes

    def _extract_frame_at(self, file_path: Path, timestamp: float) -> KeyframeInfo | None:
        """Extract a single frame at a specific timestamp."""
        if not self._ffmpeg:
            return None

        temp_path = str(file_path.with_suffix(f"_frame_{int(timestamp * 1000)}.png"))
        try:
            subprocess.run(
                [
                    self._ffmpeg, "-y", "-ss", str(timestamp),
                    "-i", str(file_path),
                    "-vframes", "1", "-q:v", "2",
                    temp_path,
                ],
                capture_output=True,
                timeout=30,
            )
            if os.path.exists(temp_path):
                img_bytes = Path(temp_path).read_bytes()
                os.unlink(temp_path)
                return KeyframeInfo(timestamp=timestamp, image_bytes=img_bytes)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _ocr_frame(self, image_bytes: bytes) -> str:
        """Run OCR on a frame image."""
        try:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR()
            result, _ = ocr(image_bytes)
            if result:
                return "\n".join(line[0] for line in result if line)
        except ImportError:
            log.warning("[VideoProcessor] rapidocr_onnxruntime not available")
        return ""

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
                context_parts.append("关键帧OCR:\n" + "\n".join(ocr_texts[:3000]))

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
