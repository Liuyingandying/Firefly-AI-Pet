"""Firefly_BiliInsight_Service —— BiliInsight 服务层 JSONL 服务

License: GPL-3.0-or-later。本服务是 BiliInsight（https://github.com/Shanoa2/BiliInsight，
GPL-3.0）services 层的独立 JSONL 封装，随本仓库整体以 GPL-3.0 发布。

协议：stdin 逐行读 JSON 请求，stdout 逐行写统一 JSON 响应。
  请求: {"action": "health" | "metadata" | "transcribe" | "frame", ...参数}
  响应: {"action", "ok", "data", "error", "latency_ms", "service", "version"}

只用 BiliInsight 的 services 层：
  - 不启动 Claude Agent SDK（本 venv 不安装 claude-agent-sdk）
  - 不使用 MCP tools（不 import bilichat.tools / bilichat.app）
  - chromadb 由 bili_compat.install_shims() 以 stub 方式解耦

transcribe 错误四分类：
  env_dependency    环境依赖失败（ffmpeg 缺失、模块导入失败）
  model             模型失败（whisper 模型下载/加载失败）
  bilibili_download B站下载失败（取流地址失败、ffmpeg 切片失败）
  asr               ASR失败（音频提取、推理阶段错误）
"""

import asyncio
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent


def _default_biliinsight_src() -> Path:
    """按布局约定寻找并列克隆的 BiliInsight/src，找不到则返回首选候选。"""
    candidates = [
        # 插件仓布局：…/<workspace>/Firefly-AI-Pet-Plugins/firefly_bili_insight_service
        SERVICE_DIR.parent.parent / "BiliInsight" / "src",
        # 独立目录布局：…/<workspace>/firefly_bili_insight_service
        SERVICE_DIR.parent / "BiliInsight" / "src",
    ]
    for candidate in candidates:
        if (candidate / "bilichat").is_dir():
            return candidate
    return candidates[0]


# BiliInsight 上游源码（GPL-3.0）：默认按上面的布局约定定位，可用环境变量
# BILIINSIGHT_SRC 显式指向任意已克隆的上游 src/ 目录。
BILIINSIGHT_SRC = Path(os.environ.get(
    "BILIINSIGHT_SRC", str(_default_biliinsight_src())))

os.chdir(SERVICE_DIR)               # .env / config.yaml / data 目录基于服务目录
sys.path.insert(0, str(SERVICE_DIR))

if not (BILIINSIGHT_SRC / "bilichat").is_dir():
    sys.stderr.write(
        f"BiliInsight source not found: {BILIINSIGHT_SRC}\n"
        "git clone https://github.com/Shanoa2/BiliInsight.git next to the "
        "repository that contains this directory (so <workspace>/BiliInsight/src "
        "exists), or set BILIINSIGHT_SRC to a cloned src/ directory.\n"
    )
    sys.exit(2)

sys.path.insert(0, str(BILIINSIGHT_SRC))

from bili_compat import install_shims  # noqa: E402
_shims = install_shims()

from bilichat.services.bilibili import BilibiliService, parse_video_id  # noqa: E402
from bilichat.services.subtitle import parse_time  # noqa: E402
from bilichat.services.video_download import get_video_download_service  # noqa: E402
from bilichat.services.frame_extract import get_frame_extract_service  # noqa: E402
from bilichat.services.transcription import TranscriptionService  # noqa: E402

SERVICE_NAME = "Firefly_BiliInsight_Service"
VERSION = "0.1.0"
FRAMES_OUT = SERVICE_DIR / "data" / "frames_out"


class ActionError(Exception):
    def __init__(self, err_type: str, message: str):
        super().__init__(message)
        self.err_type = err_type


# ---------------------------------------------------------------- handlers

async def act_health(args: dict) -> dict:
    checks = {}
    checks["bilibili_api_reachable"] = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.bilibili.com/x/web-interface/zone",
                            headers={"User-Agent": "Mozilla/5.0"})
            # 收到任何 HTTP 响应（含 412 限流）都证明网络可达；
            # 仅连接错误/超时视为不可达
            checks["bilibili_api_reachable"] = True
            checks["bilibili_api_http_status"] = r.status_code
    except Exception as e:
        checks["bilibili_api_error"] = str(e)[:120]
    checks["ffmpeg"] = shutil.which("ffmpeg") is not None
    checks["ffprobe"] = shutil.which("ffprobe") is not None
    try:
        import faster_whisper  # noqa: F401
        checks["faster_whisper_import"] = True
    except Exception as e:
        checks["faster_whisper_import"] = False
        checks["faster_whisper_error"] = str(e)[:120]
    checks["chromadb_stub_active"] = "chromadb" in _shims or \
        getattr(sys.modules.get("chromadb"), "__name__", "").startswith("chromadb")
    checks["claude_agent_sdk_absent"] = "claude_agent_sdk" not in sys.modules
    ok = checks["ffmpeg"] and checks["ffprobe"] and checks["bilibili_api_reachable"]
    return {"status": "healthy" if ok else "degraded", "checks": checks}


async def act_metadata(args: dict) -> dict:
    video_id = args.get("video_id")
    if not video_id:
        raise ActionError("invalid_args", "video_id is required")
    meta = await BilibiliService().get_video_info(video_id)
    return {
        "bvid": meta.bvid,
        "aid": meta.aid,
        "cid": meta.cid,
        "title": meta.title,
        "duration": meta.duration,
        "duration_formatted": meta.duration_formatted,
        "owner": meta.owner_name,
        "tags": meta.tags,
        "cover_url": meta.cover_url,
        "has_subtitle_flag": meta.has_subtitle,
        "subtitle_check_failed": meta.subtitle_check_failed,
    }


async def _preflight_asr() -> None:
    """把环境/模型两类失败在进入真实转写前单独归类。"""
    if shutil.which("ffmpeg") is None:
        raise ActionError("env_dependency", "ffmpeg not found on PATH")
    try:
        from bilichat.config import settings
        cfg = settings.providers.transcription
        model_name = cfg.get("model", "small")
        extra = cfg.get("extra", {}) or {}
        from faster_whisper import WhisperModel
        WhisperModel(model_name, device=extra.get("device", "cpu"),
                     compute_type=extra.get("compute_type", "int8"))
    except ActionError:
        raise
    except ModuleNotFoundError as e:
        raise ActionError("env_dependency", f"missing dependency: {e.name}")
    except Exception as e:
        raise ActionError("model", f"whisper model load failed: {str(e)[:200]}")


def _classify_transcribe_error(e: Exception) -> ActionError:
    msg = str(e)
    if isinstance(e, ActionError):
        return e
    if isinstance(e, ModuleNotFoundError):
        return ActionError("env_dependency", f"missing dependency: {e.name}")
    if ("FFmpeg" in msg and ("下载" in msg or "切片" in msg)) or \
       "无法获取视频下载地址" in msg or "没有可用的视频流" in msg or \
       type(e).__name__ == "ResponseCodeException":
        return ActionError("bilibili_download", msg[:300])
    if "音频提取" in msg:
        return ActionError("asr", msg[:300])
    return ActionError("internal", f"{type(e).__name__}: {msg[:300]}")


async def act_transcribe(args: dict) -> dict:
    video_id = args.get("video_id")
    if not video_id:
        raise ActionError("invalid_args", "video_id is required")
    bvid = parse_video_id(video_id)
    await _preflight_asr()          # env_dependency / model 先行归类
    svc = TranscriptionService()
    try:
        segments = await svc.transcribe(
            bvid,
            start_time=float(args.get("start_time", 0)),
            end_time=float(args["end_time"]) if args.get("end_time") is not None else None,
        )
    except Exception as e:
        raise _classify_transcribe_error(e)
    segs = [{"start": s.start, "end": s.end, "text": s.content} for s in segments]
    return {
        "bvid": bvid,
        "engine": "bilichat.services.transcription / faster-whisper",
        "window": {"start_time": args.get("start_time", 0), "end_time": args.get("end_time")},
        "segment_count": len(segs),
        "chars": sum(len(s["text"]) for s in segs),
        "segments": segs,
    }


async def act_frame(args: dict) -> dict:
    video_id = args.get("video_id")
    ts_raw = args.get("timestamp")
    if not video_id or ts_raw is None:
        raise ActionError("invalid_args", "video_id and timestamp are required")
    ts = parse_time(str(ts_raw))
    service = BilibiliService()
    meta = await service.get_video_info(video_id)
    if ts >= meta.duration:
        raise ActionError("invalid_args", f"timestamp {ts}s beyond duration {meta.duration}s")
    segment_path = await get_video_download_service().download_for_frame(
        video_id=meta.bvid, timestamp=ts, cid=meta.cid, buffer_seconds=2.0)
    frame = await get_frame_extract_service().extract_frame_from_video_segment(
        video_path=segment_path, target_timestamp=ts, segment_start=max(0, ts - 2.0))
    FRAMES_OUT.mkdir(parents=True, exist_ok=True)
    out_path = FRAMES_OUT / f"{meta.bvid}_{int(ts)}s.jpg"
    import base64 as _b64
    with open(out_path, "wb") as f:
        f.write(_b64.b64decode(frame.base64_data))
    data = {
        "bvid": meta.bvid, "timestamp_s": ts,
        "frame_path": str(out_path), "bytes": frame.file_size,
        "media_type": frame.media_type, "video_title": meta.title,
    }
    if args.get("include_base64"):
        data["base64"] = frame.base64_data
    return data


HANDLERS = {
    "health": act_health,
    "metadata": act_metadata,
    "transcribe": act_transcribe,
    "frame": act_frame,
}


async def dispatch(req: dict) -> dict:
    t0 = time.perf_counter()
    action = req.get("action")
    envelope = {"action": action, "ok": False, "data": None, "error": None,
                "latency_ms": None, "service": SERVICE_NAME, "version": VERSION}
    if action not in HANDLERS:
        envelope["error"] = {"type": "unknown_action",
                             "message": f"known actions: {sorted(HANDLERS)}"}
        return envelope
    try:
        envelope["data"] = await HANDLERS[action](req)
        envelope["ok"] = True
    except ActionError as e:
        envelope["error"] = {"type": e.err_type, "message": str(e)}
    except Exception as e:
        envelope["error"] = {"type": "internal",
                             "message": f"{type(e).__name__}: {str(e)[:300]}",
                             "traceback": traceback.format_exc()[-600:]}
    envelope["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    return envelope


async def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"action": None, "ok": False, "data": None,
                    "error": {"type": "invalid_json", "message": str(e)},
                    "latency_ms": None, "service": SERVICE_NAME, "version": VERSION}
        else:
            resp = await dispatch(req)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
