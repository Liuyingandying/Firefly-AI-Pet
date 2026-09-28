# -*- coding: utf-8 -*-
"""Firefly Voice Module 公共入口。

用法:
    from core.pipeline import convert_voice
    result = convert_voice("input.wav", speaker_model="firefly",
                           output_audio="output.wav")
    result.audio, result.sample_rate, result.seconds
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional, Union

import soundfile as sf

from core.converter import ConvertResult, MODULE_ROOT, RVCConverter

AudioLike = Union[str, Path, bytes, bytearray]

_MODULE: Optional[RVCConverter] = None


def get_module() -> RVCConverter:
    """进程级单例: 首次调用加载引擎 (首次约 20-40s, 之后复用)。"""
    global _MODULE
    if _MODULE is None:
        _MODULE = RVCConverter()
        _MODULE.register_speakers()
    return _MODULE


def _materialize(input_audio: AudioLike, tmp_dir: Path) -> Path:
    """把 path / bytes 统一落盘为引擎可读的 wav 文件路径。"""
    if isinstance(input_audio, (bytes, bytearray)):
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / "_upload_buffer.wav"
        audio, sr = sf.read(io.BytesIO(input_audio), dtype="float32")
        sf.write(str(tmp), audio, sr)
        return tmp
    p = Path(input_audio)
    if not p.is_absolute():
        p = (MODULE_ROOT / p) if (MODULE_ROOT / p).exists() else p
    if not p.is_file():
        raise FileNotFoundError(f"输入音频不存在: {p}")
    return p


def convert_voice(
    input_audio: AudioLike,
    speaker_model: str = "firefly",
    output_audio: Optional[Union[str, Path]] = None,
    pitch: Optional[int] = None,
    index_rate: Optional[float] = None,
) -> ConvertResult:
    """Firefly 语音转换主接口。

    :param input_audio:   wav/flac/mp3 等文件路径, 或原始音频 bytes
    :param speaker_model: config.yaml 中注册的说话人名 (默认 firefly)
    :param output_audio:  可选输出路径; 给定则同时写出 wav
    :param pitch:         变调半音 (男声→流萤 建议尝试 +12; None=用注册默认)
    :param index_rate:    检索特征混合比例 (None=用注册默认)
    :return:              ConvertResult(audio, sample_rate, seconds, speaker)
    """
    module = get_module()
    in_path = _materialize(input_audio, MODULE_ROOT / "output")
    result = module.convert(in_path, speaker_model, f0_up_key=pitch, index_rate=index_rate)
    if output_audio is not None:
        out = Path(output_audio)
        if not out.is_absolute():
            out = MODULE_ROOT / out
        module.save(result, out)
    return result


def list_speakers():
    return {name: spec.display_name for name, spec in get_module().speakers.items()}
