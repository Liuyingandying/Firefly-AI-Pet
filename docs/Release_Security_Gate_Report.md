# Firefly_AI_Pet Release Step 0 — Repository Safety Gate Report

审计日期：2026-09-17  
范围：Git ignore 边界、发布配置脱敏模板、配置泄漏检查  
结论：**REPOSITORY SAFETY GATE: PASS**

## 1. 执行边界

本步骤未执行：

- `git add`
- `git commit`
- `git reset`
- `git clean`
- 文件删除、移动或权限修改
- 后续 staging

只修改 ignore 规则、添加脱敏模板、清除一条配置注释中的本机绝对路径，并生成本报告。

## 2. 修改文件

| 文件 | 变化 | 原因 |
|---|---|---|
| `.gitignore` | 补充敏感数据、日志、pytest 临时目录和 Python 缓存规则 | 防止 staging 前误收集本地数据 |
| `config/hardware_devices.example.json` | 新增脱敏发布模板 | 保留硬件配置 schema，不携带真实设备信息 |
| `config/voice_config.yaml` | 将一条 `E:\...` 本机路径注释替换为“见发行文档” | 移除个人机器路径；未改变配置值或语音行为 |
| `docs/Release_Security_Gate_Report.md` | 新增本报告 | 固化安全门证据 |

`config/companion.json` 未修改。

## 3. Ignore 规则变化

新增或明确以下规则：

```gitignore
runtime/
logs/
*.env
config/provider_state.json
config/hardware_devices.json
.pytest_*
.pytest_camera_*
**pycache**/
*.pyc
```

其中 `runtime/` 与 `*.pyc` 原本已存在，审计后保留；其余按本步骤明确补齐。原有 `.env`、`.env.*` 和 `!.env.example` 继续生效，发布用 `.env.example` 不受 `*.env` 规则影响。

## 4. 硬件配置模板

真实文件 `config/hardware_devices.json` 的字段结构为：

```text
prism
├── port
├── match
│   ├── vid_pid
│   └── serial
└── note

led
├── port
├── match
│   ├── vid_pid
│   └── serial
└── note
```

`config/hardware_devices.example.json` 完整保留相同设备类型、字段名称、嵌套关系和字符串字段类型，同时：

- `port` 为空，不含 COM 端口或真实设备路径；
- `vid_pid` 为空，不含真实设备标识；
- `serial` 字段保留以维持 schema，但值为空，不含序列号或 MAC；
- `note` 只使用通用设备用途说明，不含用户信息、维修记录或机器信息。

真实 `config/hardware_devices.json` 未修改、未删除，现已由精确规则明确 ignore。

## 5. 配置泄漏检查

### `config/companion.json`

- API key：未发现
- token / Authorization / Bearer：未发现
- Windows 绝对路径：未发现
- 用户目录或真实用户名：未发现
- 处理：无需修改

### `config/voice_config.yaml`

- API key：未发现
- token / Authorization / Bearer：未发现
- 用户目录或真实用户名：未发现
- 发现一条指向 `E:` 盘开发目录的说明性绝对路径；已仅替换该注释
- loopback 服务地址属于产品默认本机服务配置，不是凭据或个人路径
- 处理：配置键和值未改变

### `config/hardware_devices.example.json`

- API key / token：未发现
- 绝对路径：未发现
- 用户目录：未发现
- 真实序列号、MAC、COM 端口：未包含

## 6. 敏感文件与目录清单

以下内容不得进入发布 staging：

- `config/hardware_devices.json`：真实硬件匹配信息、端口和维护备注
- `config/provider_state.json`：本地 Provider 状态
- `.env`、`.env.*`、`*.env`：凭据与本机环境变量
- `runtime/`：Memory、Conversation、Scratchpad、运行状态、迁移数据及诊断产物
- `logs/`、`*.log`：运行日志
- `.pytest_*`、`.pytest_camera_*`、`.pytest_cache/`：测试临时数据
- `**pycache**/`、`__pycache__/`、`*.pyc`：Python 缓存和字节码
- 现有用户配置：`config/ui_settings.json`、`config/sessions.json`、`config/pet_preferences.json` 及其临时文件
- 私钥与证书：`*.pem`、`*.key`、`*.p12`、`*.pfx`

可进入后续候选 staging 的模板：

- `.env.example`
- `config/hardware_devices.example.json`

本报告不授权也未执行 staging。

## 7. 验证证据

### `git check-ignore -v --no-index`

确认命中：

| 目标 | 命中规则 |
|---|---|
| `config/hardware_devices.json` | `config/hardware_devices.json` |
| `runtime/state.json` | `runtime/` |
| `logs/release.log` | `logs/` |
| `config/provider_state.json` | `config/provider_state.json` |
| `sample.env` | `*.env` |
| `.pytest_probe/file` | `.pytest_*` |
| `.pytest_camera_probe/file` | `.pytest_camera_*` |
| `pkg/__pycache__/x.pyc` | `**pycache**/` |

`config/hardware_devices.example.json` 未命中任何 ignore 规则，结果为：

```text
TRACKABLE: config/hardware_devices.example.json
```

### `git status --ignored --short`

关键结果：

```text
?? config/hardware_devices.example.json
!! config/hardware_devices.json
!! logs/
!! runtime/
!! .pytest_camera_plugins/
!! .pytest_camera_plugins_final2/
```

因此：

- `hardware_devices.json`：已 ignore
- `runtime/`：已 ignore
- example 模板：可跟踪

验证过程中 Git 对 `runtime/memory_test_temp/pytest2`、`pytest3` 和 `runtime/memory_verify` 报告 `Permission denied`。这些目录整体已经由 `runtime/` 规则忽略；本步骤未进入、删除、修改权限或清理这些既有目录。

## 8. 发布前注意事项

1. 后续 staging 必须使用显式 allowlist，禁止 `git add .`。
2. staging 后再次检查 `git diff --cached --name-only` 和 staged 内容的凭据模式；本步骤没有执行该操作。
3. 不要使用 `-f` 强制加入 `config/hardware_devices.json`、`runtime/` 或 `logs/`。
4. 发布配置只能从 example/template 文件生成，不能复制真实本机配置进发行目录。
5. 若未来扩展硬件 schema，应同时更新 example，并重复字段树一致性和敏感值扫描。
6. `voice_config.yaml` 当前仍是未跟踪候选文件；进入后续 allowlist 前应确认其是否属于发布默认配置。
7. ACL 不可读的 runtime 测试目录应保持忽略；不得为发布目的执行递归删除或权限接管。

## 9. 最终状态

**REPOSITORY SAFETY GATE: PASS**

