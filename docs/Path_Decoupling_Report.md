# Firefly_AI_Pet Path Decoupling Report

日期：2026-09-17  
阶段：Phase 2 最小修复 + Phase 3 验证  
状态：**PATH DECOUPLING: READY**

## 1. 结果

已移除 Release staging 阻断的三个本机绝对路径耦合：

- `core/plugin_loader.py`
- `core/plugin_api.py`
- `core/bili_insight_client.py`

Firefly 不再隐式依赖任何 E 盘目录。managed plugins 与 BiliInsight 都采用：

```text
环境变量
  ↓
config/path_config.yaml
  ↓
%LOCALAPPDATA%/FireflyAI/plugins 下的可移植默认目录
```

目录不存在时不创建外部能力、不启动子进程，也不影响 Firefly 核心 import/启动。

## 2. 修改文件

### 新增

- `core/path_config.py`
  - Qt-free、fail-safe 的小型路径解析器。
  - 只负责 optional external roots，不建立通用配置框架。
- `config/path_config.yaml`
  - `plugin_root`
  - `bili_insight_root`
  - 默认空值，使用用户数据目录默认路径。
- `tests/test_path_decoupling.py`
  - 环境变量优先级
  - 用户配置优先级
  - 缺失/损坏配置回退
  - 缺插件目录
  - 缺 BiliInsight 服务
  - legacy Bili 环境变量兼容
  - 目标生产文件绝对路径扫描
- `docs/Path_Decoupling_Audit.md`
- `docs/Path_Decoupling_Report.md`

### 修改

- `core/plugin_loader.py`
  - 删除硬编码 legacy E 盘 root。
  - 新增主根环境变量 `FIREFLY_PLUGIN_ROOT`。
  - 保留 `FIREFLY_PLUGIN_PATH` 作为附加搜索路径。
- `core/plugin_api.py`
  - docstring 示例改为 `%LOCALAPPDATA%\FireflyAI\plugins\<name>`。
- `core/bili_insight_client.py`
  - 新增 `FIREFLY_BILI_INSIGHT_ROOT`。
  - 兼容旧 `FIREFLY_BILI_SERVICE_DIR`。
  - 默认目录改为 `%LOCALAPPDATA%\FireflyAI\plugins\bili-insight`。
- 六个 video/Bili 真实集成测试
  - 不再自行硬编码 E 盘服务目录。
  - skip 条件与生产 `DEFAULT_SERVICE_DIR` 使用同一真源。
- `tools/hardware_ports.py`
  - 将注释中的真实 USB serial 示例替换为通用占位符；逻辑未改变。

## 3. 路径优先级

### Managed plugins

1. `FIREFLY_PLUGIN_ROOT`
2. `config/path_config.yaml` 的 `paths.plugin_root`
3. `%LOCALAPPDATA%\FireflyAI\plugins`

现有 `FIREFLY_PLUGIN_PATH` 继续作为额外根列表，其内容不能绕过 managed plugin allowlist。

### BiliInsight

1. `FIREFLY_BILI_INSIGHT_ROOT`
2. 兼容环境变量 `FIREFLY_BILI_SERVICE_DIR`
3. `config/path_config.yaml` 的 `paths.bili_insight_root`
4. `%LOCALAPPDATA%\FireflyAI\plugins\bili-insight`

两个环境变量同时存在时，新名称优先。

### 相对配置值

YAML 中的相对路径以 Firefly 项目/安装根为基准；绝对路径仍允许用户显式配置，但发行源码不再携带开发机路径。

## 4. Graceful degradation

### Managed plugins 缺失

- `discover_roots()` 返回配置根。
- 根目录不存在时 `load()` 跳过扫描并返回空 manifest 列表。
- registry 保持为空，managed external capability 表现为 unavailable。
- Companion、Memory、Conversation、PageLens 等核心 import 不受影响。

### BiliInsight 缺失

- 缺 `service.py`：抛出既有 `BiliServiceError(kind="env_dependency")`。
- 缺 service Python：同样归一化为 `env_dependency`。
- 不启动无效子进程。
- Video 调用方沿用既有 graceful failure 文案/能力门，不影响 Firefly 核心。

### Voice Module

- 已通过 `config/voice_config.yaml` 配置 loopback URL。
- 无文件系统外部根依赖。
- 服务不存在时现有客户端返回 unavailable / timeout / error，不影响文字聊天。

### Hardware

- 配置路径保持仓库相对 `config/hardware_devices.json`；真实文件被 ignore。
- 缺配置返回空配置；缺设备返回 `None`。
- Prism 缺失时 supervisor 跳过桥；外设失败不阻断 pet。
- 本阶段未修改硬件解析或桥接行为。

## 5. 验证结果

### 专项与关联回归

```text
116 passed, 8 skipped
```

覆盖：

- path_config
- plugin loader / extension API / plugin allowlist
- BiliInsight client
- Video capability gate
- video frame / chat entry / study / session / timestamp

8 个 skip 均为显式真实外部服务/设备不可用的集成用例，符合 optional external 能力边界。

### 无 E 盘模拟启动

在独立子进程中把两个新环境变量指向不存在的临时目录，并执行 Firefly import、插件加载和 Bili health：

```text
firefly_import=PASS
plugins=EXTERNAL_UNAVAILABLE
bili_insight=GRACEFUL_UNAVAILABLE
```

### 生产路径扫描

精确扫描模式：

```text
E:\
C:\Users\
E:/Firefly
C:/Users/
```

扫描范围包括 app、core、memory、providers、ui、voice_client、extensions、配置和发布启动/硬件工具。

结果：

```text
production_personal_absolute_paths=0
target_file_personal_paths=0
```

HTTPS、loopback URL 与 `file://` 协议说明不属于 Windows 个人文件路径，未作为失败。

### 静态检查

- 变更 Python 文件通过 `py_compile`。
- `config/path_config.yaml` 解析通过。
- `git diff --check` 未发现新增空白错误。
- import smoke 显示 `ui/agent_launcher.py` 两处既有反斜杠 docstring `SyntaxWarning`；与本次路径耦合无关，不影响验证结果。

## 6. API 与行为边界

未改变：

- Plugin API / manifest / managed allowlist
- `FIREFLY_PLUGIN_PATH`
- `BiliInsightClient` 公共方法与 JSONL 协议
- `BiliServiceError` 分类
- Video 业务逻辑
- Voice 行为
- Hardware 行为
- UI
- Memory / Conversation / Learning 数据逻辑

## 7. Git 状态约束

- 未执行 `git add`。
- 未执行 commit。
- 未删除任何文件。
- 最终 `git diff --cached --name-only` 数量：0。

## 8. 后续 Release 建议

重新运行 Release staging 的生产路径和隐私预扫描。确认缓存区仍为空后，从 manifest Batch A 开始按显式文件白名单 staging；不得使用 `git add .`。

## 9. 最终状态

**PATH DECOUPLING: READY**

