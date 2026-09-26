# Voice Setup — 语音能力部署指南

> 面向想给 Firefly 加上语音（TTS + RVC 变声 + 扬声器播放）的用户。
> 链路：LLM 回复 → `VoiceAnnouncer`（宿主）→ `voice_client`（本仓库）
> → **Voice Module 服务**（插件仓）→ edge-tts → RVC → sounddevice 声卡。

## 1. 服务端源码在哪

**[Firefly-AI-Pet-Plugins → `firefly_voice/voice_module/`](https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins/tree/main/firefly_voice/voice_module)**

完整源码（api / core / audio / vendor 引擎快照 / 测试 / 启动脚本）都在插件仓，
本仓库 `voice_client/` 只是宿主侧客户端——两侧通过 `127.0.0.1:8300` HTTP 解耦，
互不 import。

## 2. 安装（三步）

```powershell
git clone https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins.git
cd Firefly-AI-Pet-Plugins\firefly_voice\voice_module

# ① Python 3.10 venv + GPU torch（必须用 cu121 wheel，先于 requirements）
py -3.10 -m venv venv
.\venv\Scripts\python.exe -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121

# ② 其余依赖（requirements 中 torch 已注释，不会覆盖上面的 cu121 版本）
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# ③ 模型（4 个文件，不随仓库分发——许可证门）
#    下载后放入 models/，并核对 models/manifest.json 中的 SHA256
```

模型来源与放置路径、缺模型时的报错样式，见插件仓
[`firefly_voice/voice_module/models/README.md`](https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins/blob/main/firefly_voice/voice_module/models/README.md)。

## 3. 启动与验证

```powershell
# 启动（检查 python/模型/依赖/端口 → 启动 → 等 /health → READY）
powershell -ExecutionPolicy Bypass -File .\start_voice.ps1 -PythonPath .\venv\Scripts\python.exe

# 验证
Invoke-RestMethod http://127.0.0.1:8300/health
```

## 4. 配置 Firefly 宿主

编辑 `%LOCALAPPDATA%\FireflyAI\plugins\firefly_voice\config.yaml`：

```yaml
voice:
  enabled: true
  auto_play: true      # false = 仅消息上的 🔊 播放按钮
  server:
    url: http://127.0.0.1:8300
  service:
    root: <你机器上 voice_module 目录的绝对路径>
    python: <该 venv 的 python.exe 绝对路径>
    script: api/server.py
```

改完重启 Firefly（配置在启动时读取）。之后每条 LLM 回复完成即自动朗读
（或点击消息上的播放按钮）。

## 5. 常见问题

| 症状 | 处理 |
|---|---|
| `parselmouth ... 页面文件太小` / cuDNN engine 错误 / `numpy _ArrayMemoryError` / 显存充足的 CUDA OOM | 多为 Windows 提交内存不足；跑 `scripts\check_environment.py` 看 `system.commit`，释放内存后重试 |
| 服务连续失败后一直 OOM | 重启服务进程（面板「停止/启动」或 `stop_voice.ps1` + `start_voice.ps1`） |
| `NoAudioReceived` | 测试请求勿用 Git Bash curl 发中文（会变 `?`）；用 PowerShell `Invoke-RestMethod` |
| 启动报模型不存在 | 按 models/README.md 放置模型并校验 SHA256 |

完整排查手册：插件仓 `firefly_voice/voice_module/README.md` 的 Troubleshooting 一节。
