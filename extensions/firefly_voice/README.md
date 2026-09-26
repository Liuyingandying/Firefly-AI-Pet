# Firefly Voice Plugin

把 Firefly 语音能力（TTS + RVC 声线转换）纳入插件生态的**状态管理插件**。

## 1. 功能介绍

- **能力开关**：Voice Enabled 总开关（默认开——语音能力默认存在）
- **自动播放开关**：Auto Play（默认关，v1.3 设计：回复完成仅显示播放按钮，点击才朗读）
- **服务状态**：实时显示 Voice Service ONLINE/OFFLINE 与延迟
- **服务显式启停**：启动 / 停止 / 重启外部语音服务（**绝不自动启动**）
- **测试播放**：一键经宿主链路试听
- **Voice Settings 面板**：以上全部经插件系统暴露（`open()`），不改聊天界面

## 2. 架构

```
┌─────────────────────────────────────────────────────────────┐
│ 聊天链路（宿主所有，插件不参与）                                │
│ LLM FINAL ─► VoiceAnnouncer ─► voice_client ─► HTTP :8300    │
│                                    ▲               │         │
│                                    │         voice_module     │
│                                    │         （外部服务进程）   │
│                                    │               │         │
│                                    │          TTS(edge) ► RVC │
│                                    │               │         │
│                                    │         sounddevice 播放  │
└────────────────────────────────────┼──────────────────────────┘
                                     │ 读配置（env > 用户配置 > 旧文件 > 默认）
┌────────────────────────────────────┼──────────────────────────┐
│ VoicePlugin（本插件，只做能力状态管理）                          │
│  status() / health_check() ──► VoiceServiceManager ─► TCP 探测 │
│  enable() / disable() / set_auto_play() ──► 写用户插件配置     │
│  start_service() / stop_service() / restart_service()          │
│  test_play() ──► 宿主 voice_client.speak（显式用户动作）        │
└───────────────────────────────────────────────────────────────┘
```

职责边界：**插件负责能力管理（开关/状态/服务生命周期），不负责聊天逻辑、LLM 调用与 Agent 决策**。

## 3. 安装方式

将本目录复制到 Firefly 运行时插件根后重启宿主：

```
%LOCALAPPDATA%\FireflyAI\plugins\firefly_voice\
```

- 插件根可用 `FIREFLY_PLUGIN_ROOT` 环境变量或 `config/path_config.yaml` 的 `paths.plugin_root` 重定向
- 开发环境可用 `FIREFLY_PLUGIN_PATH` 指向本目录的父目录
- 宿主启动时经白名单 + AST 只读探测自动发现（manifest id：`firefly-voice`）

## 4. 配置方式

用户插件配置（唯一可写落点）：

```
%LOCALAPPDATA%\FireflyAI\plugins\firefly_voice\config.yaml
```

解析优先级：**环境变量 > 用户插件配置 > 旧 `config/voice_config.yaml` > 默认值**

```yaml
voice:
  enabled: true            # 能力总开关（默认 true）
  auto_play: false         # 自动朗读（默认 false，v1.3）
  max_speak_chars: 1600    # 超长回复截断朗读
  server:
    url: http://127.0.0.1:8300
  service:                 # 外部服务路径（机器相关，需自行填写）
    root: ""               #   服务工程目录（含 api/server.py）
    python: ""             #   服务解释器（含依赖的环境）
    script: api/server.py
```

- 环境变量覆盖：`FIREFLY_VOICE_ENABLED` / `FIREFLY_VOICE_AUTO_PLAY` / `FIREFLY_VOICE_URL`
- 首次加载自动**一次性迁移**旧配置：url/超时/情绪/截断字段继承；`enabled` 按新默认 `true` 落盘，`auto_play` 保持 `false`

## 5. 服务启动方式

语音服务是**独立外部进程**，由用户显式启动。**服务端完整源码已开源**于
[Firefly-AI-Pet-Plugins → `firefly_voice/voice_module/`](https://github.com/Liuyingandying/Firefly-AI-Pet-Plugins/tree/main/firefly_voice/voice_module)
（含安装、模型获取与 Troubleshooting；模型权重因许可证外置）：

1. **推荐**：Voice Settings 面板 → 「启动语音服务」（使用配置中的 `service.root/python`）
2. 手动命令行（voice_module 目录内）：
   ```powershell
   .\venv\Scripts\python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 8300
   # 或
   .\venv\Scripts\python.exe api\server.py
   ```
3. 冷启动模型加载约 **7-50 秒**；`GET /health` 返回 200 即就绪
4. 停止：面板「停止语音服务」或 voice_module 内 `stop_voice.ps1`
   （仅终止本插件启动的 PID 或持有 8300 端口的 python 进程）

**资源控制策略**：服务不随宿主启动、宿主退出不代管服务生命周期。

## 6. 无 GPU 降级说明

- 服务端推理设备由服务工程配置决定（`engine.device: auto`——有 GPU 用 CUDA fp16，否则 CPU fp32）
- CPU 模式可用但合成明显变慢；服务未就绪/未启动时，**宿主与聊天零影响**：
  - 插件状态显示 OFFLINE；`test_play` 与播放按钮返回明确的失败原因（`disabled` / `unavailable`）
  - 聊天链路永不阻塞、永不报错（voice_client「永不抛异常」设计）
- 无外部服务时仍可用：能力开关、状态查看、全部非语音功能

## 7. 隐私说明

- **本插件不采集、不存储、不传输任何用户数据**：只读写本地配置文件与本地回环端口
- 语音文本仅发往 `127.0.0.1:8300`（本机服务）；TTS 合成由服务侧调用在线 TTS 引擎完成，**RVC 声线转换与音频播放全部在本机**
- 服务路径等机器相关信息只存在于用户数据目录，**不进版本控制、不进插件仓**
- 本插件不含任何模型权重、音频输出或用户配置；插件源码不含 API 密钥与本机绝对路径
