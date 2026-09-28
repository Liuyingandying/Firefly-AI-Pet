# Firefly Voice Module

把 LLM 回复文本变成 **流萤 (Firefly)** 的声音：
[edge-tts](https://github.com/rany2/edge-tts)（文本→基准人声）
→ [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)（声线转换）
→ `sounddevice`（Windows 声卡播放）。

以本机 HTTP 服务形式运行在 **127.0.0.1:8300**，为
[Firefly AI Pet](https://github.com/Liuyingandying/Firefly-AI-Pet) 提供语音能力，
也可独立使用。

> 源码 100% 包含在本仓库；仅模型权重因许可证原因外置（见 [Models](#models)）。
> 已验证环境：Windows 11 / Python 3.10 / RTX 4060 Laptop 8GB / torch 2.5.1+cu121。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查：设备/已加载说话人 |
| POST | `/voice/speak` | 文本→TTS→RVC→（可选）扬声器播放 |
| POST | `/voice/queue/stop` | 停止当前播放 |
| GET | `/voice/queue` | 播放队列状态 |
| POST | `/voice/convert` | 音频文件变声（上传 WAV） |

`/voice/speak` 请求体：

```json
{ "text": "你好，我是流萤。", "play": true, "emotion": "neutral" }
```

- `play=true`：边转边播（sounddevice 队列），返回 JSON 分段元数据
- `play=false`：不播放，直接返回拼接好的 `audio/wav`（40kHz，已过 RVC）
- `emotion`：`happy/sad/comfort/excited/neutral`，省略则按中文关键词自动推断

## Requirements

- **Windows** 10/11（sounddevice/WASAPI 路径按 Windows 验证）
- **Python 3.10**（Golden: 3.10.21）
- **NVIDIA GPU**（Golden: RTX 4060 8GB；建议显存 ≥ 6GB；CPU 模式存在但未验证）
- **torch 2.5.1+cu121**（必须用 cu121 wheel，见下面安装）
- **FFmpeg/ffprobe** 在 PATH 上（`winget install Gyan.FFmpeg`）
- **Voice Models**（4 个文件，见 [Models](#models)，不随仓库分发）
- NVIDIA 驱动 ≥ 531.14（wheel 内含 CUDA runtime，无需装 CUDA Toolkit）

## Installation

```powershell
git clone https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins.git
cd Firefly-AI-Pet-Plugins\firefly_voice\voice_module

# 1) Python 3.10 venv
py -3.10 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip

# 2) GPU torch —— 必须先于 requirements 安装（PyPI 直装会得到 CPU 版）
.\venv\Scripts\python.exe -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121

# 3) 其余依赖（requirements 中 torch 行已注释，不会覆盖步骤 2）
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Models

模型权重**不包含在本仓库**（许可证门：角色声模型 AGPL-3.0 + 版权归 miHoYo，禁止再分发）。
需要手动获取 4 个文件并按路径放入：

```
models\firefly\firefly-chinese.pth          # 55MB  → HF Waterwzy/RVC-firefly-finetuning
models\firefly\firefly-chinese_v2.index     # 31MB  → 同上（成对）
models\hubert_base.pt                       # 190MB → HF lj1995/VoiceConversionWebUI
models\rmvpe.pt                             # 181MB → HF lj1995/VoiceConversionWebUI
```

每个文件的 **SHA256、大小、许可证状态** 见
[`models/manifest.json`](models/manifest.json)；获取/校验/缺模型时的报错说明见
[`models/README.md`](models/README.md)。

一键校验：

```powershell
.\venv\Scripts\python.exe scripts\check_environment.py --models-only
```

## Start

```powershell
# 方式 A：脚本（检查 python/模型/依赖/端口 → 启动 → 等 /health → READY）
powershell -ExecutionPolicy Bypass -File .\start_voice.ps1 -PythonPath .\venv\Scripts\python.exe

# 方式 B：手动
.\venv\Scripts\python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8300
```

就绪标志：日志出现 `Firefly Voice Service 就绪: speakers=['firefly'] device=cuda:0`
（冷启动约 7-50s）。停止：`.\stop_voice.ps1`（只停自己启动的进程）。

## Verify

> 中文测试文本**不要用 Git Bash curl 直接发**——命令行编码可能把中文变成 `?`，
> 服务端会报 `NoAudioReceived`。用 PowerShell 或 Python。

PowerShell：

```powershell
# GET /health
Invoke-RestMethod http://127.0.0.1:8300/health
# 期望: status=ok device=cuda:0 speakers.firefly=流萤 (Firefly)

# POST /voice/speak（本机扬声器出声）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8300/voice/speak `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes('{"text":"你好，我是流萤。","play":true}'))
```

Python：

```python
import json, urllib.request
payload = {"text": "你好，我是流萤。", "play": True}
req = urllib.request.Request(
    "http://127.0.0.1:8300/voice/speak",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
print(json.loads(urllib.request.urlopen(req, timeout=300).read().decode("utf-8"))["queue"])
# 播放期间 queue.playing 非 null
```

环境全检（torch/CUDA/模型/端口/系统提交内存 13 项）：

```powershell
.\venv\Scripts\python.exe scripts\check_environment.py
```

## Connect to Firefly

编辑 Firefly 宿主的用户插件配置
`%LOCALAPPDATA%\FireflyAI\plugins\firefly_voice\config.yaml`：

```yaml
voice:
  enabled: true
  auto_play: true        # true=回复完成后自动朗读; false=聊天消息上出现 🔊 播放按钮
  server:
    url: http://127.0.0.1:8300
  service:               # 宿主可代为启动/停止本服务
    root: D:\FireflyVoice\voice_module                        # 本目录（voice_module）绝对路径
    python: D:\FireflyVoice\voice_module\venv\Scripts\python.exe
    script: api/server.py
```

| 字段 | 填什么 |
|---|---|
| `server.url` | 服务地址，默认 `http://127.0.0.1:8300` |
| `service.root` | **你机器上** `voice_module` 目录的绝对路径 |
| `service.python` | 该 venv 的 `python.exe` 绝对路径 |
| `service.script` | 保持 `api/server.py`（相对 root） |

宿主侧链路：LLM 回复 → `VoiceAnnouncer`（auto_play 门）→ `FireflyVoiceClient`
→ 本服务。插件与宿主之间只通过 HTTP + 配置解耦，互相不 import。

## Troubleshooting

以下均为实测出现过的故障（详见主仓库 `docs/voice/MEMORY_REQUIREMENTS.md`）：

1. **Windows Commit/页面文件不足**（最常见）——报错有三种面目：
   - 启动期：`ImportError: DLL load failed while importing parselmouth: 页面文件太小`（WinError 1455）
   - 推理期：`RuntimeError: GET was unable to find an engine to execute this computation`（cuDNN）
   - 推理期：`numpy.core._exceptions._ArrayMemoryError: Unable to allocate ...`

   先跑 `scripts\check_environment.py` 看 `system.commit`；Commit Free 低于
   Limit 的 ~15% 即高危，关闭大内存程序/增大页面文件后重试。
2. **显存充足的"CUDA out of memory"**——CUDA 分配同样消耗系统提交内存；
   显存大量空闲仍报 OOM 时优先查 Commit，不要急着换显卡/调小模型。
3. **服务连续 OOM 后可能需要重启进程**——进程经历过内存不足失败后，即使资源
   恢复也可能持续失败（CUDA 上下文/缓存损坏）；重启服务即愈，无需重启机器。
4. **`TORCH_CUDNN_V8_API_DISABLED=1`** 是**临时绕过**（进程级环境变量），
   不是默认要求：仅在内存充足 + 新进程仍复现 cuDNN engine 错误时使用
   （`start_voice.ps1 -CudnnWorkaround` 可注入），不要写入持久配置。
5. **`edge_tts.exceptions.NoAudioReceived`**——发出去的文本被客户端编码损坏
   （典型：Git Bash curl 中文变 `?`）。用 PowerShell `Invoke-RestMethod` 或
   Python UTF-8 请求体。
6. **启动报 `FileNotFoundError: 说话人 firefly 模型不存在`**——模型没放好，
   按 [Models](#models) 放置并用 `--models-only` 校验 SHA256。

## License

- `api/ core/ audio/ config/ tests/ scripts/`（本模块自研源码）与仓库根 LICENSE 一致
- `vendor/Retrieval-based-Voice-Conversion-WebUI-main/`：上游 RVC 快照，MIT（零修改，随附 LICENSE）
- 模型权重：见 `models/manifest.json` 逐项许可证（`firefly-chinese` 为 AGPL-3.0，
  角色声版权归 miHoYo/HoYoverse，禁止再分发）
