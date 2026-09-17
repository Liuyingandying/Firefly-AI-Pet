# Firefly_AI_Pet Path Decoupling Audit

日期：2026-09-17  
阶段：Phase 1 — 只读审计  
基线：修复前工作树；未 staging、未 commit、未删除文件

## 1. 审计范围

扫描以下生产面及其发布支撑入口：

- `app.py`、`state_broker.py`、`start_pet.ps1`
- `core/`、`memory/`、`providers/`、`ui/`、`voice_client/`
- `extensions/`
- 发布启动链使用的 `tools/firefly_runtime_supervisor.py`、硬件桥和 `hardware_ports.py`
- `config/`

同时扫描 tests、tools smoke/benchmark 和 docs 产物，用于区分生产耦合与仅测试/日志路径。

目标模式：

```text
E:\
C:\Users\
<drive>:/
```

URL、`file://` 协议示例和 loopback 地址单独审阅，不把 `https://`、`ws://127.0.0.1` 误判为文件系统路径。

## 2. 生产代码命中

| 文件 | 位置 | 当前用途 | 风险 |
|---|---:|---|---|
| `core/plugin_loader.py` | 35 | managed plugin 的旧开发目录兼容根 | 换机不可复现；隐含 E 盘依赖 |
| `core/plugin_api.py` | 4 | 插件 API docstring 示例 | 泄漏开发机目录布局 |
| `core/bili_insight_client.py` | 29 | BiliInsight 服务默认目录 | 未配置时错误地探测固定 E 盘 |

生产代码中未发现 `C:\Users\<真实用户>` 路径。

其余扫描命中均为 URL、协议说明或通用解析注释，不属于本机绝对路径，例如 Provider HTTPS 端点、PageLens websocket loopback、PDF `file://` 解析说明。

## 3. 外部能力路径分类

### A. 必须配置化

#### Managed plugins

- 当前正式默认根已经是 `%LOCALAPPDATA%\FireflyAI\plugins`。
- 仍附加硬编码 `E:\Firefly_AI_Private_Plugins` 作为 legacy discovery root。
- 已存在 `FIREFLY_PLUGIN_PATH`，但它是追加搜索路径，不是明确的主根配置。
- 修复要求：新增 `FIREFLY_PLUGIN_ROOT` 主根；用户配置其次；默认使用用户数据插件目录。旧开发目录不再隐式发现。
- 缺目录语义：返回空插件集合；Firefly 核心继续启动。

#### BiliInsight

- 当前 `FIREFLY_BILI_SERVICE_DIR` 未设置时回退到固定 E 盘。
- 修复要求：新增 `FIREFLY_BILI_INSIGHT_ROOT`，兼容旧环境变量名称；用户配置其次；默认使用 `%LOCALAPPDATA%\FireflyAI\plugins\bili-insight`。
- 缺 `service.py` 或 Python runtime 时保持现有 `BiliServiceError(kind="env_dependency")`，由 Video 能力降级处理，不启动子进程、不影响 Companion 核心。

### B. 开发默认路径

- `tools/benchmark_codex_short_talk.py`：开发 benchmark workspace。
- `tools/benchmark_codex_transport.py`：开发机 npm/codex shim 说明。
- `tools/smoke_phase8c*.py`、`tools/smoke_codex_short_talk.py`：命令帮助中的仓库示例。
- 多个 `tools/test_phase*.py`：`E:/Work`、`E:/x`、`C:/ws` 等隔离测试夹具。
- OCR / video real tests：`C:/Windows/Fonts/...`，属于 Windows 系统字体探测，不是用户目录。

这些路径不在生产启动链的运行决策中。发布 staging 应继续排除 benchmark/smoke 临时工具或将其作为 dev-only 单独处理。

### C. 仅日志/测试

- `docs/phase8c_benchmark_results.json`：历史 benchmark 记录，包含当时的开发机 CLI 路径；不属于生产配置，不应进入 RC 安装内容。
- `tests/test_bili_video_reader.py`、video 系列 tests：真实集成测试的服务存在性/skip 路径。
- `tools/test_phase9c_native_handoff.py`：显式测试 `C:\Users\<用户名>\...` 路径转义，属于测试样例，不进入生产源码扫描通过条件。
- tests 中的 `file:///C:/papers/...`、`E:/Work`、`C:/fake`：合成测试数据。

## 4. Voice Module

- 文件系统绝对路径：未发现。
- 服务地址来自 `config/voice_config.yaml`，当前为 loopback URL。
- `voice_client/config.py` 提供缺文件/损坏配置的默认值。
- Voice 服务缺失时客户端返回 unavailable / timeout / error，现有测试锁定“不影响聊天”。
- 分类：已配置化，不需要引入 Voice Module 磁盘根目录。

## 5. Hardware

- `tools/hardware_ports.py` 使用仓库相对 `config/hardware_devices.json`，未硬编码用户目录或 E 盘。
- 用户真实硬件配置已被 `.gitignore` 排除，发布只带 `hardware_devices.example.json`。
- 端口由显式配置、USB serial、VID:PID 依次解析；缺配置返回空映射，缺设备返回 `None`。
- supervisor 在 Prism 不存在时跳过 Prism bridge；LED bridge为可选外设进程，失败不阻断 pet。
- 分类：相对配置 + 可选硬件；无本机绝对路径修复需求。

## 6. 最小修复范围

拟修改：

- 新增 `config/path_config.yaml`：只包含外部能力路径键，默认留空。
- 新增 `core/path_config.py`：小型、Qt-free、fail-safe loader。
- 修改 `core/plugin_loader.py`：移除硬编码 legacy root，采用环境变量 → 用户配置 → 用户数据默认目录。
- 修改 `core/plugin_api.py`：使用可移植示例。
- 修改 `core/bili_insight_client.py`：采用环境变量 → 用户配置 → 用户数据默认目录。
- 新增/调整路径解耦专项测试。

不修改 UI、插件协议、BiliInsight JSONL 协议、Video 业务规则、Voice 行为或 Hardware 解析规则。

## 7. 风险

1. 过去依赖隐式 E 盘插件目录的开发机将不再自动发现该目录；应设置 `FIREFLY_PLUGIN_ROOT` 或写入 `config/path_config.yaml`。
2. BiliInsight 旧环境变量需保持兼容，避免现有部署突然失效。
3. 配置文件缺失、无效 YAML、空字符串和不存在路径都必须 fail-safe，不允许阻断 import 或应用启动。
4. tests/tools 中的合成绝对路径应保留其测试价值，但生产扫描必须排除明确的测试与历史证据范围。

## 8. Phase 1 状态

**PATH DECOUPLING AUDIT: COMPLETE**

