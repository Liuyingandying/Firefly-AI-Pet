# -*- coding: utf-8 -*-
"""speak_text(): Firefly 表达层主管道。

LLM 文本
  → EmotionParser (显式情绪 或 中文关键词推断)
  → 分句 (core/splitter)
  → 逐句: edge-tts(语速/音量) → RVC(变调, 复用 v1.0 核心, 不改) → energy 增益
  → (play=True) 增量入 AudioQueue: 第 1 句转完即播, 后续边转边播
  → (play=False) 全部拼接返回 wav
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np
import soundfile as sf

import logging

from core.emotion import EmotionParams, get_params, parse_emotion
from core.expression import ExpressionPlan
from core.pipeline import MODULE_ROOT, convert_voice
from core.splitter import split_sentences

SEGMENT_GAP_S = 0.12  # 句间留白, 韵律自然

log = logging.getLogger(__name__)


# ---------------- v1.3.1: 播放会话 / cancellation token ----------------

class SpeechSession:
    """一次 /voice/speak 的取消令牌: 分句循环每个阶段前检查 cancelled。"""

    def __init__(self) -> None:
        self.session_id = uuid.uuid4().hex[:12]
        self.cancel_event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()


_active_lock = threading.Lock()
_active_session: Optional["SpeechSession"] = None


def start_session() -> SpeechSession:
    """注册新的活跃会话 (服务端单渲染锁下同时至多一个)。"""
    global _active_session
    with _active_lock:
        _active_session = SpeechSession()
        return _active_session


def cancel_active_session() -> Optional[str]:
    """取消当前活跃会话; 返回其 session_id (无活跃会话返回 None)。"""
    with _active_lock:
        session = _active_session
    if session is None:
        return None
    session.cancel()
    return session.session_id


@dataclass
class Segment:
    text: str
    emotion: str
    audio: np.ndarray
    sample_rate: int
    tts_ms: int = 0
    vc_ms: int = 0

    @property
    def duration_s(self) -> float:
        return len(self.audio) / max(self.sample_rate, 1)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "emotion": self.emotion,
            "duration_s": round(self.duration_s, 2),
            "tts_ms": self.tts_ms,
            "vc_ms": self.vc_ms,
        }


@dataclass
class SpeakResult:
    text: str
    emotion: str
    emotion_source: str
    params: EmotionParams
    segments: List[Segment] = field(default_factory=list)
    total_ms: int = 0
    cancelled: bool = False
    gap_s: float = SEGMENT_GAP_S       # v1.4: expression 模式下段尾已含停顿, 置 0

    @property
    def sample_rate(self) -> int:
        return self.segments[0].sample_rate if self.segments else 40000

    def concat_audio(self) -> np.ndarray:
        """按顺序拼接全部句段 (句间留白, expression 模式段尾已自带)。"""
        chunks, sr = [], self.sample_rate
        gap = np.zeros(int(sr * self.gap_s), dtype=np.float32)
        for i, seg in enumerate(self.segments):
            if i:
                chunks.append(gap)
            chunks.append(np.asarray(seg.audio, dtype=np.float32))
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def to_wav_bytes(self) -> bytes:
        buf = io.BytesIO()
        sf.write(buf, self.concat_audio(), self.sample_rate, format="wav")
        return buf.getvalue()

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "emotion": self.emotion,
            "emotion_source": self.emotion_source,
            "params": {"speed": self.params.speed, "pitch": self.params.pitch, "energy": self.params.energy},
            "segments": [s.to_dict() for s in self.segments],
            "total_ms": self.total_ms,
            "audio_duration_s": round(sum(s.duration_s for s in self.segments), 2),
        }


def apply_energy(audio: np.ndarray, energy: float) -> np.ndarray:
    """情绪能量增益 + 峰值限幅保护 (不动 RVC 核心)。"""
    out = np.asarray(audio, dtype=np.float32) * float(energy)
    peak = np.abs(out).max() if out.size else 0.0
    if peak > 0.98:
        out *= 0.98 / peak
    return out


def tts_synthesize(text: str, params: EmotionParams, voice: str = "zh-CN-XiaoxiaoNeural") -> bytes:
    """edge-tts 合成, 返回 mp3 bytes。(须在线程池/独立线程中调用, 内部有 asyncio.run)"""
    import edge_tts  # noqa: PLC0415

    async def _run() -> bytes:
        communicate = edge_tts.Communicate(text, voice, rate=params.tts_rate, volume=params.volume)
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    return asyncio.run(_run())


def _synthesize_segment(text: str, params: EmotionParams, speaker_model: str, pitch: Optional[int],
                        session: Optional[SpeechSession] = None) -> Segment:
    t0 = time.perf_counter()
    mp3 = tts_synthesize(text, params)
    tts_ms = int((time.perf_counter() - t0) * 1000)
    if session is not None and session.cancelled:
        raise _CancelledError()

    tmp = Path(tempfile.gettempdir()) / f"firefly_seg_{int(time.time() * 1000)}_{id(mp3) % 99999}.mp3"
    tmp.write_bytes(mp3)
    try:
        t1 = time.perf_counter()
        result = convert_voice(str(tmp), speaker_model=speaker_model, pitch=params.pitch if pitch is None else pitch)
        vc_ms = int((time.perf_counter() - t1) * 1000)
        if session is not None and session.cancelled:
            raise _CancelledError()   # RVC 完成后、入队前最后一道闸
        audio = apply_energy(result.audio, params.energy)
        return Segment(text=text, emotion=params.name, audio=audio, sample_rate=result.sample_rate,
                       tts_ms=tts_ms, vc_ms=vc_ms)
    finally:
        tmp.unlink(missing_ok=True)


class _CancelledError(RuntimeError):
    pass


def speak_text(
    text: str,
    speaker_model: str = "firefly",
    emotion: Optional[str] = None,
    play: bool = False,
    priority: int = 1,
    incremental: bool = True,
    session: Optional[SpeechSession] = None,
    expression: Optional[ExpressionPlan] = None,
) -> SpeakResult:
    """文本 → 情绪解析 → 分句 → 逐句 TTS+VC → (可选)队列播放。

    :param play: True 时边转边播 (AudioQueue); False 时仅拼接返回
    :param priority: 播放优先级 (>=10 视为紧急, 抢占当前播放)
    :param incremental: play=True 时逐段入队 (首句延迟最小化)
    :param session: v1.3.1 取消令牌; 取消后立即停止生成并入队
    :param expression: v1.4 表达层计划 (emotion/intensity/style → 参数+标点+停顿)
    """
    t0 = time.perf_counter()
    if expression is not None:
        # v1.4 Expression Layer: 模板参数 + 句尾标点映射, 跳过关键词推断
        name, source = expression.emotion, "expression"
        params = expression.params
        sentences = [expression.punctuate(s) for s in split_sentences(text)]
    else:
        name, source = parse_emotion(text, emotion)
        params = get_params(name)
        sentences = split_sentences(text)
    if not sentences:
        raise ValueError("空文本")

    result = SpeakResult(text=text, emotion=name, emotion_source=source, params=params)
    if expression is not None:
        result.gap_s = 0.0               # 停顿已按模板填充在每段尾部

    queue = None
    if play:
        from audio.audio_queue import AudioItem, get_audio_queue  # noqa: PLC0415

        queue = get_audio_queue()

    cancelled = False
    for sentence in sentences:
        if session is not None and session.cancelled:
            cancelled = True
            break
        log.info("tts_input: %r", sentence)   # v1.3.1 调试: 实际送入 edge-tts 的文本
        try:
            seg = _synthesize_segment(sentence, params, speaker_model, pitch=None, session=session)
        except _CancelledError:
            cancelled = True
            break
        if expression is not None and expression.pause_ms > 0 and len(seg.audio):
            tail = np.zeros(int(seg.sample_rate * expression.pause_ms / 1000), dtype=np.float32)
            seg.audio = np.concatenate([np.asarray(seg.audio, dtype=np.float32), tail])
        result.segments.append(seg)
        if queue and incremental:
            queue.enqueue(
                AudioItem(audio=seg.audio, sample_rate=seg.sample_rate, text=seg.text,
                          emotion=seg.emotion, priority=priority,
                          session_id=session.session_id if session else None),
            )

    result.total_ms = int((time.perf_counter() - t0) * 1000)
    result.cancelled = cancelled
    return result
