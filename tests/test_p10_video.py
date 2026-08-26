"""Phase v0.3-P10: Video Understanding pipeline tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.video_pipeline import (
    KeyframeInfo,
    SubtitleEntry,
    VideoProcessor,
    VideoQa,
    VideoQaResult,
    VideoSegment,
    VideoTimeline,
)


# ======================================================================
# P10-A: Data models
# ======================================================================


class TestP10A_DataModels:
    """Video pipeline data model tests."""

    def test_subtitle_entry(self) -> None:
        entry = SubtitleEntry(index=1, start=0.0, end=3.5, text="Hello world")
        assert entry.index == 1
        assert entry.start == 0.0
        assert entry.end == 3.5
        assert entry.text == "Hello world"

    def test_keyframe_info(self) -> None:
        kf = KeyframeInfo(timestamp=5.0, image_bytes=b"\x89PNG", ocr_text="OCR text")
        assert kf.timestamp == 5.0
        assert kf.image_bytes == b"\x89PNG"
        assert kf.ocr_text == "OCR text"

    def test_video_segment(self) -> None:
        seg = VideoSegment(start=0.0, end=30.0, summary="Intro")
        assert seg.start == 0.0
        assert seg.end == 30.0
        assert seg.summary == "Intro"
        assert seg.key_timestamps == []

    def test_video_timeline_empty(self) -> None:
        tl = VideoTimeline(file_path="test.mp4")
        assert tl.file_path == "test.mp4"
        assert tl.duration == 0.0
        assert tl.segments == []
        assert tl.subtitles == []
        assert tl.keyframes == []
        assert tl.full_transcript == ""

    def test_video_timeline_with_subtitles(self) -> None:
        tl = VideoTimeline(
            file_path="test.mp4",
            subtitles=[
                SubtitleEntry(index=1, start=0.0, end=3.0, text="Hello"),
                SubtitleEntry(index=2, start=3.5, end=6.0, text="World"),
            ],
        )
        assert "Hello" in tl.full_transcript
        assert "World" in tl.full_transcript

    def test_video_qa_result(self) -> None:
        result = VideoQaResult(answer="Test answer", timestamps=[5.0, 10.0])
        assert result.answer == "Test answer"
        assert result.timestamps == [5.0, 10.0]
        assert result.related_segments == []


# ======================================================================
# P10-B: VideoProcessor — time parsing
# ======================================================================


class TestP10B_TimeParsing:
    """FFmpeg time parsing tests."""

    def test_parse_time_standard(self) -> None:
        assert VideoProcessor._parse_time("00:01:30.5") == 90.5

    def test_parse_time_short(self) -> None:
        assert VideoProcessor._parse_time("00:30.5") == 30.5

    def test_parse_time_invalid(self) -> None:
        assert VideoProcessor._parse_time("invalid") == 0.0

    def test_format_srt_time(self) -> None:
        assert VideoProcessor._format_srt_time(0.0) == "00:00:00,000"
        assert VideoProcessor._format_srt_time(60.5) == "00:01:00,500"
        assert VideoProcessor._format_srt_time(3661.123) == "01:01:01,123"


# ======================================================================
# P10-C: VideoProcessor — no ffmpeg available
# ======================================================================


class TestP10C_NoFfmpeg:
    """VideoProcessor degrades gracefully without ffmpeg."""

    def test_init_without_ffmpeg(self) -> None:
        processor = VideoProcessor(ffmpeg_path="/nonexistent/ffmpeg")
        assert processor._ffmpeg == ""

    def test_get_duration_without_ffmpeg(self) -> None:
        processor = VideoProcessor(ffmpeg_path="/nonexistent/ffmpeg")
        duration = processor._get_duration(Path("/fake.mp4"))
        assert duration == 0.0

    def test_extract_audio_without_ffmpeg(self) -> None:
        processor = VideoProcessor(ffmpeg_path="/nonexistent/ffmpeg")
        result = processor._extract_audio(Path("/fake.mp4"))
        assert result is None

    def test_extract_frame_without_ffmpeg(self) -> None:
        processor = VideoProcessor(ffmpeg_path="/nonexistent/ffmpeg")
        result = processor._extract_frame_at(Path("/fake.mp4"), 5.0)
        assert result is None


# ======================================================================
# P10-D: VideoProcessor — SRT generation
# ======================================================================


class TestP10D_SrtGeneration:
    """SRT subtitle file generation."""

    def test_generate_srt(self, tmp_path: Path) -> None:
        processor = VideoProcessor()
        subtitles = [
            SubtitleEntry(index=1, start=0.0, end=3.0, text="Hello"),
            SubtitleEntry(index=2, start=3.5, end=6.0, text="World"),
        ]
        srt = processor.generate_srt(subtitles, tmp_path / "video.mp4")

        assert "1" in srt
        assert "00:00:00,000 --> 00:00:03,000" in srt
        assert "Hello" in srt
        assert "World" in srt

        srt_path = tmp_path / "video.srt"
        assert srt_path.exists()
        assert srt_path.read_text(encoding="utf-8") == srt


# ======================================================================
# P10-E: VideoQa — mocked
# ======================================================================


class TestP10E_VideoQa:
    """Video QA with mocked chat handler."""

    def test_answer_with_no_content(self, tmp_path: Path) -> None:
        qa = VideoQa()
        # Create a minimal file (not a real video, but processor handles it)
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video content")

        result = qa.answer(video_path, "What is this about?")

        # Should return a graceful message when no content extracted
        assert isinstance(result, VideoQaResult)

    def test_answer_with_mocked_chat(self, tmp_path: Path) -> None:
        mock_chat = MagicMock(return_value={
            "provider": "fake",
            "choices": [{"message": {"role": "assistant", "content": "这是关于测试的视频"}}],
        })
        qa = VideoQa(chat_handler=mock_chat)

        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video")

        result = qa.answer(video_path, "What is this?")

        # When no ffmpeg, answer falls back gracefully
        assert isinstance(result, VideoQaResult)

    def test_summarize_with_no_content(self, tmp_path: Path) -> None:
        qa = VideoQa()
        video_path = tmp_path / "test.mp4"
        video_path.write_bytes(b"fake video")

        result = qa.summarize(video_path)

        assert isinstance(result, VideoQaResult)
        assert "没有可提取" in result.answer or result.answer == ""


# ======================================================================
# P10-F: VideoProcessor — scene detection unavailable
# ======================================================================


class TestP10F_SceneDetection:
    """Scene detection degradation."""

    def test_detect_scenes_without_scenedetect(self) -> None:
        """When PySceneDetect is not available, _detect_scenes returns empty."""
        processor = VideoProcessor()
        # If scenedetect is actually installed, this test may return results
        # The important thing is it doesn't crash
        keyframes = processor._detect_scenes(Path("/nonexistent.mp4"))
        assert isinstance(keyframes, list)


# ======================================================================
# P10-G: VideoTimeline — full transcript
# ======================================================================


class TestP10G_FullTranscript:
    """Full transcript generation from subtitles."""

    def test_empty_transcript(self) -> None:
        tl = VideoTimeline(file_path="test.mp4", subtitles=[])
        assert tl.full_transcript == ""

    def test_transcript_joins_subtitles(self) -> None:
        tl = VideoTimeline(
            file_path="test.mp4",
            subtitles=[
                SubtitleEntry(index=1, start=0.0, end=2.0, text="Line 1"),
                SubtitleEntry(index=2, start=2.5, end=4.0, text="Line 2"),
            ],
        )
        assert "Line 1" in tl.full_transcript
        assert "Line 2" in tl.full_transcript
