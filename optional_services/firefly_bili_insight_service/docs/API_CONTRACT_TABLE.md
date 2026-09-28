# API 契约表（Firefly 客户端 ↔ BiliInsight Service）

对照源码：`Firefly-AI-Pet/core/bili_insight_client.py` ↔ 本仓库 `service.py`
（实测日期 2026-09-26，匿名模式，公开视频 BV1s54y1n7Ev）。

## 传输方式

- Firefly 通过 **subprocess** 启动 `<service>/.venv/Scripts/python.exe -X utf8 service.py`，
  cwd=服务目录；一次调用一个子进程，服务按行无状态处理。
- stdin 一行 UTF-8 JSON 请求 → stdout 一行 UTF-8 JSON 响应。无 HTTP、无进程内 import。

## 统一响应信封

```json
{"action": "...", "ok": true, "data": {...}, "error": null,
 "latency_ms": 361, "service": "Firefly_BiliInsight_Service", "version": "0.1.0"}
```

失败时 `ok=false`、`data=null`、`error={type, message[, traceback]}`。

## Action 契约

| action | 客户端方法 | 服务端实现 | 输入 | 输出 data 字段 | 实测（匿名） |
|---|---|---|---|---|---|
| health | `health()` | `act_health`：httpx 探测 B站 API 可达性 + `shutil.which` 检查 ffmpeg/ffprobe + faster-whisper 可导入性 | `{}` | `status`(healthy/degraded), `checks{bilibili_api_reachable, bilibili_api_http_status, ffmpeg, ffprobe, faster_whisper_import, chromadb_stub_active, claude_agent_sdk_absent}` | ✅ PASS 1921ms |
| metadata | `metadata(video_id)` | `act_metadata` → bilibili-api `Video.get_info/get_tags/get_subtitle` | `{"video_id": "BV..."}`（BV/AV/URL 均可） | `bvid, aid, cid, title, duration, duration_formatted, owner, tags[], cover_url, has_subtitle_flag, subtitle_check_failed` | ✅ PASS 361ms |
| transcribe | `transcribe(video_id, start_time?, end_time?)` | `act_transcribe` → 下载片段（bilibili-api 取流 + ffmpeg 远程切片）→ ffmpeg 提取 16kHz WAV → faster-whisper 本地 ASR | `{"video_id", "start_time": 0, "end_time": null}`（秒；end_time=null=全片） | `bvid, engine, window{start_time,end_time}, segment_count, chars, segments[{start,end,text}]` | ✅ PASS 17820ms（0–30s 冷跑，6 段/160 字）；全片历史 PoC 259s/237 段/3913 字 |
| frame | `frame(video_id, timestamp, include_base64=false)` | `act_frame` → 取 metadata 校验时长 → 下载 ±2s 片段 → ffmpeg 抽帧 → JPEG 落盘 `data/frames_out/` | `{"video_id", "timestamp": "2:44"\|"164", "include_base64": bool}` | `bvid, timestamp_s, frame_path, bytes, media_type, video_title[, base64]` | ✅ PASS 3567ms |

时间戳解析（`parse_time`）支持 `"90"`（秒）、`"1:30"`（MM:SS）、`"1:30:00"`、`"1分30秒"`。

## 错误分类

服务端 `error.type`（service.py 主动归类）：

| type | 含义 | 触发例 |
|---|---|---|
| `env_dependency` | 环境依赖缺失 | ffmpeg 不在 PATH、Python 包缺失、BiliInsight 源码未克隆 |
| `model` | Whisper 模型失败 | 模型下载/加载失败（首次运行需网络） |
| `bilibili_download` | B站下载失败 | 取流地址失败、ffmpeg 切片失败、不存在的 BV、无权限视频 |
| `asr` | ASR 阶段失败 | 音频提取/推理错误 |
| `invalid_args` | 参数缺失/非法 | 缺 video_id、timestamp 超出视频时长 |
| `unknown_action` | 未知 action | — |
| `internal` | 兜底 | 未归类异常（附 traceback 尾部） |
| `invalid_json` | stdin 行非 JSON | — |

客户端（bili_insight_client.py）额外归一化的失败：`timeout`（超过该 action 预算）、
`crashed`（子进程退出且无 JSONL 响应）、`env_dependency`（service.py/python.exe 不存在）。
所有失败统一以 `BiliServiceError` 抛给调用方。

客户端每 action 超时预算：health 30s / metadata 120s / frame 180s / transcribe 900s。

## 语义要点（实测确认）

1. **`transcribe` = 本地 ASR（faster-whisper），不是 B站官方字幕**。
   `engine` 字段固定为 `bilichat.services.transcription / faster-whisper`。
   官方字幕内容抓取在上游 BiliInsight 中存在（`SubtitleService`），但本服务未接线。
2. **匿名下 `has_subtitle_flag` 不可靠**：B站字幕接口需要登录，匿名时返回
   `has_subtitle_flag=false, subtitle_check_failed=true`（2026-09-26 实测命中）。
   该字段只作参考，不影响 transcribe/frame。
3. Firefly 侧消费字段：`bili_video_reader` 用 metadata 的
   `title/owner/duration_formatted/duration/tags` + transcribe 的
   `segments/segment_count/chars`；`video_frame_vision` 用 frame 的
   `base64/frame_path`。
