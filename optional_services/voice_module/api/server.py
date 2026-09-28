# -*- coding: utf-8 -*-
"""Firefly Voice Service — FastAPI 封装。

启动:
    cd voice_module
    python api/server.py          (或 uvicorn api.server:app --port 8300)

接口:
    GET  /health                 健康检查 (设备/已加载说话人)
    GET  /voice/speakers         说话人列表
    POST /voice/convert          音频→音频 转换 (multipart: file + speaker_id + pitch)
    POST /voice/tts_convert      文本→流萤语音 (需可选依赖 edge-tts)
"""

from __future__ import annotations

import io
import logging
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_ROOT))

import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core.pipeline import MODULE_ROOT, convert_voice, get_module

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("firefly.voice")

_render_lock = threading.Lock()  # RVC 推理非线程安全, 单卡串行


@asynccontextmanager
async def lifespan(app: FastAPI):
    module = get_module()
    for name in module.speakers:
        module.load_speaker(name)
    logger.info("Firefly Voice Service 就绪: speakers=%s device=%s", list(module.speakers), module.device)
    yield


app = FastAPI(title="Firefly Voice Module", version="1.0.0", lifespan=lifespan)


def _wav_response(result, elapsed_ms: int) -> Response:
    buf = io.BytesIO()
    sf.write(buf, result.audio, result.sample_rate, format="wav")
    return Response(
        content=buf.getvalue(),
        media_type="audio/wav",
        headers={"X-Convert-Ms": str(elapsed_ms), "X-Speaker": result.speaker},
    )


@app.get("/health")
def health():
    module = get_module()
    return {
        "status": "ok",
        "device": module.device,
        "speakers": {k: v.display_name for k, v in module.speakers.items()},
        "loaded": list(module._loaded),
    }


@app.get("/voice/speakers")
def speakers():
    return {"speakers": {k: v.display_name for k, v in get_module().speakers.items()}}


@app.post("/voice/convert")
async def voice_convert(
    file: UploadFile = File(...),
    speaker_id: str = Form("firefly"),
    pitch: int = Form(0, description="变调半音, 男声→流萤可试 12"),
    index_rate: float = Form(0.75, ge=0, le=1),
):
    import time

    data = await file.read()
    if not data:
        raise HTTPException(400, "空音频文件")
    try:
        with _render_lock:
            t0 = time.perf_counter()
            result = convert_voice(data, speaker_model=speaker_id, pitch=pitch, index_rate=index_rate)
            elapsed = int((time.perf_counter() - t0) * 1000)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return _wav_response(result, elapsed)


@app.post("/voice/tts_convert")
async def tts_convert(
    text: str = Form(..., description="要朗读的文本 (LLM 回复)"),
    voice: str = Form("zh-CN-XiaoxiaoNeural"),
    speaker_id: str = Form("firefly"),
    pitch: int = Form(0),
):
    """文字 → TTS → Voice Conversion → 流萤音色 wav。依赖可选包 edge-tts。"""
    try:
        import edge_tts  # noqa: PLC0415
    except ImportError:
        raise HTTPException(501, "未安装 edge-tts: pip install edge-tts 后可用文字直转")
    import tempfile
    import time

    tmp = Path(tempfile.gettempdir()) / f"firefly_tts_{int(time.time()*1000)}.mp3"
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(tmp))
    try:
        with _render_lock:
            t0 = time.perf_counter()
            result = convert_voice(str(tmp), speaker_model=speaker_id, pitch=pitch)
            elapsed = int((time.perf_counter() - t0) * 1000)
    finally:
        tmp.unlink(missing_ok=True)
    return _wav_response(result, elapsed)


# ============================================================
# v1.1 表达层接口 (纯增量, 不影响上方 v1.0 接口)
# ============================================================


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, description="LLM 回复文本")
    emotion: str | None = Field(None, description="happy/sad/comfort/excited/neutral/worried/shy, 省略则自动推断")
    intensity: float | None = Field(None, ge=0.0, le=1.0, description="v1.4 情绪强度 0~1 (默认 0.5=模板原值)")
    speaking_style: str | None = Field(None, description="v1.4 说话风格 (firefly/gentle)")
    speaker_id: str = "firefly"
    play: bool = Field(False, description="True=经 AudioQueue 在本机扬声器播放(边转边播); False=仅返回拼接 wav")
    priority: int = Field(1, ge=0, le=100, description="播放优先级, >=10 视为紧急抢占")
    voice: str = Field("zh-CN-XiaoxiaoNeural", description="底层 TTS 音色")


@app.post("/voice/speak")
async def voice_speak(req: SpeakRequest):
    """LLM 文本 → 情绪解析 → 分句 → 逐句 TTS+VC → (可选)队列播放。

    play=false (默认): 返回 audio/wav (全句拼接, 情绪参数已应用)
    play=true       : 边转边播, 返回 JSON 分段元数据 (供桌宠/前端联动)
    """
    from core.speech import speak_text, start_session  # noqa: PLC0415

    session = start_session()   # v1.3.1: 播放会话, /voice/queue/stop 可整体取消
    expression = None
    if req.emotion or req.speaking_style or req.intensity is not None:
        # v1.4 Expression Layer: emotion/intensity/speaking_style → 参数+标点+停顿
        from core.expression import resolve_expression  # noqa: PLC0415

        expression = resolve_expression(req.emotion or "neutral", req.intensity, req.speaking_style)
    try:
        result = await run_in_threadpool(
            speak_text,
            req.text,
            speaker_model=req.speaker_id,
            play=req.play,
            priority=req.priority,
            session=session,
            expression=expression,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except ImportError as e:
        raise HTTPException(501, f"缺少依赖: {e}")
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    if not req.play:
        return Response(
            content=result.to_wav_bytes(),
            media_type="audio/wav",
            headers={
                "X-Emotion": result.emotion,
                "X-Emotion-Source": result.emotion_source,
                "X-Segments": str(len(result.segments)),
                "X-Total-Ms": str(result.total_ms),
                "X-Session-Id": session.session_id,
                "X-Cancelled": str(result.cancelled),
                "X-Voice-Pitch": str(result.params.pitch),
                "X-TTS-Rate": result.params.tts_rate,
            },
        )

    from audio.audio_queue import get_audio_queue  # noqa: PLC0415

    payload = result.to_dict()
    payload["session_id"] = session.session_id
    payload["cancelled"] = result.cancelled
    payload["intensity"] = req.intensity
    payload["speaking_style"] = req.speaking_style
    payload["queue"] = get_audio_queue().status()
    return payload


@app.get("/voice/queue")
def queue_status():
    from audio.audio_queue import get_audio_queue  # noqa: PLC0415

    return get_audio_queue().status()


@app.post("/voice/queue/stop")
def queue_stop():
    """v1.3.1 真正取消: 当前会话标记取消(后台合成停止+晚到段丢弃) + 停当前播 + 清待播队列。"""
    from audio.audio_queue import get_audio_queue  # noqa: PLC0415
    from core.speech import cancel_active_session  # noqa: PLC0415

    queue = get_audio_queue()
    session_id = cancel_active_session()
    stopped = queue.stop_current()
    cleared = queue.clear()
    if session_id:
        queue.cancel_session(session_id)   # 幂等: 丢弃该会话晚到段
    return {"stopped": bool(stopped or cleared), "cancelled_session": session_id, "cleared": cleared}


@app.post("/voice/queue/clear")
def queue_clear():
    """清空待播队列。"""
    from audio.audio_queue import get_audio_queue  # noqa: PLC0415

    return {"cleared": get_audio_queue().clear()}




if __name__ == "__main__":
    import yaml

    with open(MODULE_ROOT / "config.yaml", encoding="utf-8") as f:
        svc = yaml.safe_load(f).get("service", {})
    import uvicorn

    uvicorn.run(app, host=svc.get("host", "127.0.0.1"), port=int(svc.get("port", 8300)))
