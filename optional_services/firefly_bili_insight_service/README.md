# firefly_bili_insight_service — B站视频阅读服务

[Firefly AI Pet](https://github.com/Liuyingandying/Firefly-AI-Pet) 的 **B站视频阅读后端**，
随 [Firefly-AI-Pet-Plugins](https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins)
统一发布：一个 stdin/stdout JSONL 微服务，为 Firefly 提供四类能力——
`health`（自检）、`metadata`（视频信息）、`transcribe`（**本地 faster-whisper 语音转写**）、
`frame`（指定时间点抽帧）。

Firefly 侧通过 subprocess + JSONL 与本服务通信（无 HTTP、无进程内依赖），
因此本目录（GPL-3.0）与 Firefly 主仓（MIT）、本仓库其余 MIT 插件保持清晰的许可证边界
（multi-license 说明见仓库根 [README License](../README.md#license) 与
[`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)）。

> License: GPL-3.0-or-later。本服务是
> [Shanoa2/BiliInsight](https://github.com/Shanoa2/BiliInsight)（GPL-3.0）services 层的
> 独立 JSONL 封装；上游源码不进本仓库，运行时按目录约定定位（见下文安装步骤）。

---

## 先回答你最关心的三个问题

1. **需要安装 B站桌面客户端吗？** —— **不需要。** 全链路只依赖
   [bilibili-api-python](https://github.com/Nemo2011/bilibili-api)（HTTP API）+ ffmpeg。
2. **需要登录 B站账号吗？** —— **不需要（可选）。**
   公开视频的 metadata / transcribe / frame **匿名全部可用**（2026-09-26 实测）。
   登录只改善两件事：metadata 里 `has_subtitle` 字幕标志的准确度（B站字幕接口要求
   登录，匿名时该标志返回 `subtitle_check_failed=true`）、以及观看需要登录的视频。
3. **`transcribe` 是 B站官方字幕吗？** —— **不是。** 它是**本地 ASR**
   （faster-whisper small，int8，CPU）：下载视频片段 → ffmpeg 提取音频 → 本地推理。
   不上传音频到任何云端，与是否登录无关。详见
   [docs/AUTH_AND_DEPS.md](docs/AUTH_AND_DEPS.md)。

## 它是怎么工作的

```
B站 URL / BV号
  → Firefly（core/bili_video_reader）
  → BiliInsightClient（subprocess + stdin/stdout JSONL）
  → 本服务 service.py
  → 上游 BiliInsight bilichat.services.*（bilibili-api + ffmpeg + faster-whisper）
  → metadata / transcript / frame
  → Firefly AI 总结 / 视觉理解
```

协议：stdin 一行 JSON 请求，stdout 一行 JSON 响应。
完整字段级契约（含实测延迟与错误分类）见
[docs/API_CONTRACT_TABLE.md](docs/API_CONTRACT_TABLE.md)。

```json
{"action": "health"}
{"action": "metadata", "video_id": "BV1s54y1n7Ev"}
{"action": "transcribe", "video_id": "BV1s54y1n7Ev", "start_time": 0, "end_time": 30}
{"action": "frame", "video_id": "BV1s54y1n7Ev", "timestamp": "2:44", "include_base64": false}
```

失败时响应 `error.type ∈ {env_dependency, model, bilibili_download, asr,
invalid_args, unknown_action, internal, invalid_json}`。

## 安装（约 10 分钟）

### 1. Python

Python **3.10+**（实测 3.13）：https://www.python.org/downloads/
Windows 安装时勾选 *Add python.exe to PATH*。

### 2. FFmpeg（必需）

- Windows：`winget install ffmpeg`，或从 https://www.gyan.dev/ffmpeg/builds/
  下载 *essentials build* 后把 `bin` 目录加入 PATH。
- macOS：`brew install ffmpeg`
- 验证：新开终端运行 `ffmpeg -version` 与 `ffprobe -version` 都有输出。
  （服务的 `health` action 也会替你检查。）

### 3. 克隆插件仓 + 上游服务层（并列放置，目录名保持默认）

```bash
git clone https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins.git   # 本服务所在插件仓
git clone https://github.com/Shanoa2/BiliInsight.git                      # 上游服务层（GPL-3.0）
```

推荐的工作区结构（`BiliInsight` 与插件仓**并列**）：

```
workspace/
├── Firefly-AI-Pet/                        # 宿主（可选）
├── Firefly-AI-Pet-Plugins/
│   └── firefly_bili_insight_service/      # ← 本目录
└── BiliInsight/
    └── src/                               # ← service.py 默认来这里找上游
```

`service.py` 默认按布局约定自动定位：优先插件仓的**父目录**下的
`BiliInsight/src`（即上图结构），兼容旧的单目录并列布局。放在别处也可以，
用环境变量显式指过去：

```bash
export BILIINSIGHT_SRC=/path/to/BiliInsight/src        # Windows: set 为 BiliInsight\src 的完整路径
```

没找到上游时服务会启动失败并在 stderr 打印克隆指引（退出码 2），不会抛出难懂的导入错误。

### 4. 创建虚拟环境并装依赖

```bash
cd Firefly-AI-Pet-Plugins/firefly_bili_insight_service
python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
# macOS / Linux:
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` 只含服务层最小依赖（不含上游的 chromadb / LLM SDK 等）。
上游 `bilichat` 直接从克隆目录 import，无需安装。

### 5. ASR 模型（transcribe 用，首次自动下载）

首次 `transcribe` 会自动从 HuggingFace 下载
[`Systran/faster-whisper-small`](https://huggingface.co/Systran/faster-whisper-small)
（MIT 许可，缓存约 464MB，位于 `~/.cache/huggingface`）。国内网络可提前设置镜像：

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

模型与语种在 `config.yaml` 调整（默认 `small / cpu / int8 / zh`）；
想换 `medium`/`large-v3` 改这一个文件即可。

## 启动与调用

服务本身由 Firefly 自动拉起（一次调用一个子进程）；手动验证时给它喂 JSONL 行：

```bash
# Windows:
scripts\verify_actions.jsonl 的每一行都会得到一行 JSON 响应：
.venv\Scripts\python.exe -X utf8 service.py < scripts\verify_actions.jsonl

# macOS / Linux:
.venv/bin/python -X utf8 service.py < scripts/verify_actions.jsonl
```

或直接跑冒烟测试（四 action 匿名实测，含一段真实下载+ASR）：

```bash
.venv/Scripts/python scripts/smoke_test.py          # Windows
.venv/bin/python scripts/smoke_test.py              # macOS / Linux
```

## 让 Firefly 指向本服务

Firefly 主仓按以下优先级定位本服务（见 `Firefly-AI-Pet/config/path_config.yaml`
与 `core/bili_insight_client.py`）：

1. 环境变量 `FIREFLY_BILI_INSIGHT_ROOT`（旧名 `FIREFLY_BILI_SERVICE_DIR` 仍兼容）
   → 指向**本目录**：`<Firefly-AI-Pet-Plugins>/firefly_bili_insight_service`
   （即 service.py 所在目录）；
2. `config/path_config.yaml` 的 `paths.bili_insight_root`；
3. 默认 `%LOCALAPPDATA%/FireflyAI/plugins/Firefly_BiliInsight_Service`。

启动契约：`FIREFLY_BILI_INSIGHT_ROOT` 目录下须有 `service.py`，解释器默认取
`<服务目录>/.venv/Scripts/python.exe`（Windows），可用 `FIREFLY_BILI_SERVICE_PYTHON`
覆盖——本目录布局与该契约完全一致。健康检查：Firefly 日志或手动执行
`{"action": "health"}`，`status=healthy` 即通。

## 登录态（可选）如何安全配置

在**本目录**复制 `.env.example` 为 `.env`，填入你自己的值（或直接设同名
环境变量，Firefly 拉起子进程时会透传）：

```
BILIBILI_SESSDATA=          # 空值 = 匿名
BILIBILI_BILI_JCT=
BILIBILI_BUVID3=
BILIBILI_BUVID4=
BILIBILI_DEDEUSERID=
```

值从你自己已登录浏览器的开发者工具（Application → Cookies → bilibili.com）复制。

**Cookie 红线**：

- ❌ 不要提交 `.env` 到 Git（本目录 `.gitignore` 已排除，但请养成习惯）；
- ❌ 不要把 `SESSDATA`/`bili_jct` 写进 issue、截图、聊天或任何示例文件；
- ✅ 只放本机 `.env` 或环境变量；泄露后到 B站「账号安全 → 退出登录」全端下线即可作废。
- 服务本身不落库、不打日志输出 Cookie（审计见 docs/AUTH_AND_DEPS.md）。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 启动即打印 `BiliInsight source not found` | 没克隆上游或路径不对：在插件仓**父目录**并列克隆 `Shanoa2/BiliInsight`，或设 `BILIINSIGHT_SRC` |
| `error.type=env_dependency`（ffmpeg） | ffmpeg/ffprobe 不在 PATH，重开终端验证 `ffmpeg -version` |
| `error.type=model` | Whisper 模型下载失败（网络）：设 `HF_ENDPOINT=https://hf-mirror.com` 后重试 |
| `error.type=bilibili_download` | BV 号不存在 / 视频需登录或会员 / 地区限制；换公开视频验证 |
| 匿名 `metadata` 里 `subtitle_check_failed=true` | 正常：B站字幕接口要登录；只影响字幕标志，不影响转写/抽帧 |
| 首次 transcribe 很慢 | 冷跑=下载片段+首次下载模型；之后有缓存（片段 24h、转写结果、帧 7 天） |
| 运行被强制中断后 transcribe 报 `asr`/音频提取失败 | 半截下载片段留在了缓存里：删除本目录 `data/cache/` 后重试 |

## 目录结构

```
service.py                  # JSONL 服务（协议 + 四 action）
bili_compat.py              # chromadb stub shim（上游零改动解耦）
config.yaml                 # ASR 模型配置（small/cpu/int8/zh）
.env.example                # 环境变量样例（登录态留空）
requirements.txt            # 最小直接依赖
LICENSE                     # GPL-3.0-or-later（本目录整体，不受仓库根 MIT 覆盖）
scripts/verify_actions.jsonl  # 手动验证请求样例
scripts/smoke_test.py       # 四 action 匿名冒烟测试
docs/API_CONTRACT_TABLE.md  # 字段级协议契约 + 实测记录
docs/AUTH_AND_DEPS.md       # 认证边界 / 客户端依赖 / ffmpeg+ASR 审计
```
