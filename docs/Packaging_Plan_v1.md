# Firefly AI Pet v1.0-rc1 — Windows 打包方案（Packaging Plan v1）

> 日期：2026-09-18
> 角色：Release Engineer
> 性质：**发布工程审计与方案**。本阶段只读审计 + 本文档输出；未执行任何 git 操作、未删除文件、未修改业务代码。
> 基线：GitHub `main` = `87a44be`（含 `3eb125a` Release v1.0-rc1）；行为基线 = 现仓库运行表现，**Memory / Conversation / Companion / Voice / Learning 行为零改变**为方案硬约束。

---

## 1. 发布目标

把仓库转换为"普通用户下载、解压、双击即可运行"的 Windows 软件：

- **形态**：单 zip 便携包（portable），Windows 10/11 x64，免安装、免管理员权限。
- **零业务行为变化**：打包只影响资源定位与进程形态，不触碰任何业务逻辑分支。
- **可选外部能力诚实降级**：Scope Freeze（`docs/Release_Scope_v1.md`）中 Optional External 的能力在包内以"缺席但可配置"状态存在。
- **rc1 不做安装器**（Inno/MSI 留待 P1，原因见 §5.4 可写目录问题）。

## 2. 启动链审计结论

实测启动链（当前仓库）：

```
start_pet.ps1
  └─ .venv/Scripts/pythonw.exe  tools/firefly_runtime_supervisor.py
       ├─ 启动 app.py（主体：单实例 QLocalServer "FireflyAIPet-SingleInstance"
       │   + Win32 互斥体 "FireflyAIPet-SingleInstance-Mutex"；托盘常驻；
       │   setQuitOnLastWindowClosed(False)）
       └─ 可选启动 2 个 ESP32 桥（独立 Job Object，设备缺席即跳过，
           一个失败绝不阻断另一个）
停止链：托盘菜单「退出」（唯一真退出）
       / tools/stop_pet.py（QLocalSocket 发 "quit"）→ stop_pet.ps1
```

关键事实（打包影响）：

1. `app.py` 用 `PROJECT_DIR = Path(__file__).resolve().parent` 定位 `assets/`、`config/`；`core/settings_manager.py`、`core/companion_config.py`、`providers/base.py`、`voice_client/config.py` 同模式（`parents[1]` = 根）。→ **onedir 下把数据文件收集进 `_internal/` 即可原样工作，零代码改动**。
2. **可写状态与只读模板同目录**：`config/pet_preferences.json`、`config/ui_settings.json`、`config/sessions.json` 由应用运行时写入（SettingsManager）。→ 决定了 rc1 必须**便携包、解压到用户可写目录**；安装到 `Program Files` 会写失败（§5.4）。
3. 运行时数据已迁移：`STATE_FILE`/`PID_FILE`/日志/记忆/会话全部走 `core/user_paths.py` → `%LOCALAPPDATA%/FireflyAI` ✅ 与包目录解耦。
4. supervisor 与两个桥脚本硬编码 `.venv` python 路径 → **冻结后该链整体退役**；rc1 包内桥不随包（硬件本就是 Optional External，app 对其完全解耦）。
5. 无 QtWebEngine 依赖（`assets/paper_reader/pdfjs/` 是浏览器扩展侧资源）；摄像头走 PySide6-Addons 的 QtMultimedia；`windows_autostart` 无 pywin32 依赖（ctypes/stdart 为主）。

## 3. 依赖审计

### 3.1 构建环境实测

- venv：**Python 3.13.9**（`E:\conda` 基座创建）——PyInstaller 需 ≥6.10（3.13 支持），建议最新 6.x + 最新 `pyinstaller-hooks-contrib`。
- 实装关键版本：PySide6 6.11.1（essentials+addons+shiboken6）、mem0ai 2.0.18、fastembed 0.8.0、qdrant_client 1.19.0、onnxruntime 1.29.0、rapidocr 3.9.2、PyMuPDF 1.28.2、numpy 2.5.2、python-docx 1.2.0、python-pptx 1.0.2、openpyxl 3.1.5、keyboard 0.13.5、requests 2.34.2、pillow 12.3.0、pyserial 3.5、faster_whisper 1.2.1、scenedetect 0.7.1（site-packages 共 198 项）。

### 3.2 依赖分类（对打包的含义）

| 类别 | 项 | 打包动作 |
|---|---|---|
| requirements 已声明 | PySide6、websockets、keyboard、mem0ai、fastembed、PyYAML、pydantic(-settings)、PyMuPDF、rapidocr、python-docx/pptx、openpyxl | 全部随包 |
| **实际 import 但 requirements 缺口** | `requests`（screen_vision 等）、`Pillow`（document_vision 等）、`pyserial`（硬件桥） | **先补进 requirements（构建机已装），再随包**；P0 前置 |
| 懒加载可选（try/except 防御） | `faster_whisper`、`scenedetect`（video_pipeline 可选 ASR/场景检测） | **不随包**——本地视频链为 Optional External，缺失时按既有路径降级 |
| OCR 离线能力 | rapidocr **模型随 wheel 附带**（`rapidocr/models/*.onnx` det/cls/rec 实测存在） | collect_data(rapidocr) → PDF/OCR **离线可用** |
| 记忆语义索引 | fastembed（首用时从网络下载 `BAAI/bge-small-zh-v1.5` 到用户数据 `models/`）+ qdrant 本地磁盘模式 + mem0（infer=False 本地） | 随包；模型预置见 §7 首次启动 |
| Qt 插件 | platforms(windows)、imageformats（**gif 动画必需**、jpeg）、multimedia（摄像头）、network（QLocalServer/LocalSocket）、tray | hooks-contrib 自动 + 显式 include multimedia/gif |
| 数据文件 | `assets/animations/*.gif`、`assets/firefly.ico`、`assets/paper_reader/pdfjs/**`、`config/` 模板 4 件、`character/**`、`extensions/pagelens_bridge/**` | collect-data 清单见 §5 |

## 4. 打包方案比选

| 维度 | A. PyInstaller onedir | B. PyInstaller onefile | C. Nuitka |
|---|---|---|---|
| Qt 兼容性 | hooks-contrib 成熟，Qt 插件自动收集；`__file__` 语义接近源码运行（数据在 `_internal/`） | 同左，但 `__file__` 指向解包临时目录 | PySide6 支持存在但 6.11 + Py3.13 组合生态证据较少 |
| 动态插件/懒加载 | hidden-import 机制成熟（mem0/qdrant/fastembed 均可声明） | 同左 | 需逐一显式声明，探测成本高 |
| 启动速度 | **快**（无解包，双击即起） | 差：数百 MB（Qt+onnxruntime+numpy）每次启动解包到 temp，首启可达数十秒 | 快（真编译） |
| 调试能力 | **好**：`_internal` 可目视核查、可 console 模式、可单文件替换验证 | 差（黑盒 temp） | 中 |
| 维护成本 | 低：改 spec/重跑即增量；社区资料最多 | 低 | **高**：编译时长长（全量数十分钟级）、工具链升级敏感、许可证形态需评估 |
| 其他 | 体量大（目录整体数百 MB） | 单文件观感好；AV 误报率最高 | 体积/性能或有优势，v1 不必要 |

**决定：方案 A（PyInstaller onedir）+ 便携 zip 分发。** 理由：唯一的 `__file__` 语义保持方案（现有 `PROJECT_DIR` 定位零改动即可工作）、启动快、可调试、维护成本最低；onefile 的启动延迟与 AV 误报对"桌面宠物常驻"形态不可接受；Nuitka 的收益（编译加速/体积）对 rc1 不构成决策权重，其工具链风险反而直接命中"零行为变化"约束。

## 5. 制品结构与 include/exclude

### 5.1 制品结构

```
FireflyAIPet-v1.0-rc1-win64-portable.zip
└─ FireflyAIPet/
   ├─ FireflyAIPet.exe            # 冻结 app.py（--windowed --onedir，托盘常驻）
   ├─ start.cmd                   # 直接启动 exe（兼容习惯 start_pet.ps1 的用户）
   ├─ README-发行说明.md           # Key 配置 / 可选外部能力 / 数据目录说明
   ├─ .env.example                # 同仓库模板
   ├─ extensions/pagelens_bridge/ # 浏览器扩展（用户手动 Load unpacked）
   ├─ install-extension.md        # 扩展安装指引
   └─ _internal/                  # PyInstaller 运行时（用户无需进入）
      ├─ assets/animations/*.gif、firefly.ico、assets/paper_reader/pdfjs/**
      ├─ config/                  # 只读模板：companion.json、voice_config.yaml、
      │                           #   path_config.yaml、hardware_devices.example.json
      ├─ character/**             # 角色资产
      ├─ .env                     # （用户创建；见 §7 首启流程注）
      └─ （Python 运行时、Qt、onnxruntime、rapidocr+models、PyMuPDF、
          qdrant/fastembed/mem0、numpy 等）
```

### 5.2 include（随包）

- 代码模块：`app.py`、`state_broker.py`、`core/**`、`memory/**`、`ui/**`、`providers/**`、`voice_client/**`（模块发现由 PyInstaller 依赖跟踪 + 显式 hidden-import 兜底）
- 数据：`assets/animations`（7 gif，动画状态机必需）、`assets/firefly.ico`、`assets/paper_reader/pdfjs`、`character/`
- 配置模板：`config/companion.json`、`config/voice_config.yaml`、`config/path_config.yaml`、`config/hardware_devices.example.json`（**不含**任何真值/个人配置）
- `extensions/pagelens_bridge/`（放包外层，便于用户装进浏览器）
- 运行库：PySide6 essentials+addons（含 QtMultimedia）、onnxruntime+rapidocr（含 onnx 模型）、PyMuPDF、qdrant_client、fastembed、mem0、websockets、keyboard、requests、Pillow、pyserial

### 5.3 exclude（不随包）

| 项 | 理由 |
|---|---|
| `tests/`、`scripts/`、`tools/`（除已随源码发布的模块外不进制品）、根目录报告 .md、debris | 非运行面 |
| `faster_whisper`、`ctranslate2`、`scenedetect`、`tokenizers`/`huggingface_hub` 中仅 ASR 用部分 | 本地视频链 = Optional External（缺 ffmpeg 也本就不可用）；可显著减小体积；缺失时按既有降级路径 |
| `runtime/`、`logs/`、`backups/`、任何 `*.sqlite/*.db` | 用户数据 / 禁运清单 |
| `.env` 真值、`config/pet_preferences.json`、`ui_settings.json`、`sessions.json`、`hardware_devices.json` 真值 | Secrets / 个人配置 |
| `MiMo*`、`_pagelens_ui_verify/`、`.pytest_*` | 个人材料 / debris |
| ESP32 桥与 supervisor | Optional External（硬件），rc1 不随包；后续可出独立可选包 |

### 5.4 External capability 清单（包内状态）

| 能力 | 包内状态 | 用户侧激活方式 |
|---|---|---|
| 屏幕视觉 / PageLens / 文档附件 / 记忆 / 学习 / 对话 / 便签 / 托盘 | **bundled，开箱即用**（LLM Key 需自配） | `.env` 或系统环境变量（TJULLM/ZHIPU/DEEPSEEK） |
| PDF OCR | **bundled 离线可用**（rapidocr 模型随包） | 无 |
| 记忆语义索引 | bundled（fastembed 模型**首启需联网一次**或预置缓存） | 见 §7 |
| Quick Tools 四插件 / Camera Vision / Video Analysis / TJU 检索 | **optional external**：插件根默认空 → 卡片不出现；`path_config.yaml` 或 `FIREFLY_PLUGIN_ROOT` 配置后激活 | 高级用户配置 |
| B 站视频链 | optional external（需外部 BiliInsight 服务 + ffmpeg） | path_config.yaml 配置 |
| Voice | optional external（客户端随包；外部 TTS 服务不随包；默认关闭） | voice_config.yaml + 外部服务 |
| ESP32 硬件 | optional external（桥不随包；app 对其完全解耦） | 后续可选包 |

## 6. 用户数据目录（打包后）

| 数据 | 位置 | 说明 |
|---|---|---|
| 运行时状态/记忆/会话/便签/建议/学习库/日志 | `%LOCALAPPDATA%\FireflyAI\`（runtime、memory、conversation、scratchpad、suggestions、learning、models、logs） | `core/user_paths.py` 既定事实，随包不变 |
| 可写偏好（pet_preferences / ui_settings / sessions） | 包目录 `config/`（= `_internal/config`） | **rc1 便携包特有**：依赖解压目录可写；P1 应迁移至用户数据目录（需小规模代码改动，另行立项） |
| `.env`（用户创建） | `_internal/.env`（冻结后 `providers/base.py` 的 `PROJECT_DIR` 落在 `_internal`）；**或**系统环境变量（优先级更高，推荐） | README 明示 |
| 外部能力根（插件/服务） | `config/path_config.yaml` 或 env 覆盖 | 空值 = 便携默认（用户数据 plugins/） |

## 7. 首次启动流程（目标体验）

1. 解压 zip 到任意**用户可写**目录（勿用 `Program Files`）→ 双击 `FireflyAIPet.exe`（或 `start.cmd`）。
2. 单实例互斥体获取 → 托盘图标出现 → 宠物层显示 → 无任何弹窗阻断。
3. 未配置 Key：聊天输入时返回既有降级文案（不崩溃、不阻塞）——现有行为，验证即可。
4. 用户按 README 配置 `.env`（放 `_internal/` 或设系统环境变量）→ 重启后 Provider Router 激活。
5. 首次记忆写入：fastembed 下载 `BAAI/bge-small-zh-v1.5`（**需联网一次**；失败则语义检索降级、记录仍权威——既有设计）。可选优化：发布前把模型缓存目录预置进 zip（约 30 MB）实现纯离线首启。
6. OCR/PageLens/PDF：即开即用（离线）。
7. 可选外部能力：默认静默缺席；README 提供配置路径。
8. 退出：托盘菜单「退出」。

## 8. 已知风险与对策

| # | 等级 | 风险 | 对策 |
|---|---|---|---|
| R1 | 高 | **SmartScreen / 杀软误报**：未签名 exe + `keyboard` 全局钩子组合 | rc1 接受（README 提示"仍要运行"）；P1 评估代码签名证书；保留 zip + SHA256 清单发布 |
| R2 | 高 | 解压到只读目录（Program Files）导致 `config/*.json` 写失败 | 发行说明显著标注"解压到用户目录"；启动时可检测 config 目录写权限并日志告警（验证项，非行为变更） |
| R3 | 中 | PySide6 6.11.1 + Python 3.13 的 hooks 覆盖（QtMultimedia 插件、gif 编解码） | 构建时显式 `--collect-all PySide6` 中按需子集 + 冒烟清单（动画 gif 播放、托盘、单实例、摄像头枚举） |
| R4 | 中 | mem0 / qdrant_local / fastembed 懒加载链 hidden-import 缺失 | 构建后运行"记忆写入→语义检索"冒烟；缺失则补 hidden-import（预期 1-2 轮迭代） |
| R5 | 中 | fastembed 首启联网下载失败（离线机器） | 语义检索自动降级（既有设计，零风险但需在 README 说明）；推荐预置模型缓存 |
| R6 | 中 | 冻结后 `.env` 位置反直觉（`_internal/`） | README 双路径（环境变量优先）；P1 引入 `app_root()` 资源定位垫片（约 6 处常量，非业务逻辑，需立项确认） |
| R7 | 低 | 包体积大（估 600-900 MB，Qt+onnxruntime+numpy 主导） | 排除 ASR 链后可接受；P1 再评估按需裁剪 Qt 模块 |
| R8 | 低 | 长路径/中文路径解压 | PyInstaller/Qt 均已支持；冒烟含中文路径解压用例 |
| R9 | 低 | `runtime/` legacy 迁移对老用户触发一次性迁移 | `user_paths` 非破坏迁移已有测试；列入冒烟 |

## 9. 构建与验收（方案级，供下一阶段执行）

- **P0 构建前置**：requirements.txt 补 `requests`、`Pillow`、`pyserial`（构建机已实装，仅声明）；锁定 `PyInstaller>=6.10` 与 `pyinstaller-hooks-contrib` 最新。
- **spec 草案要点**：`--windowed --onedir --name FireflyAIPet`；datas=assets/config 模板/character/extensions；hidden-import=mem0, qdrant_client.local, fastembed, websockets, PySide6.QtNetwork, PySide6.QtMultimedia；excludes=faster_whisper, ctranslate2, scenedetect, torch, pytest。
- **冒烟清单（冻结产物上执行）**：启动/单实例/托盘退出；动画 gif 播放；无 Key 降级文案；配置 Key 后对话一轮（不写记忆断言数据内容，仅验证连通）；记忆写入 + 语义检索命中；PDF 打开 + OCR 一页；PageLens 桥回环；Scratchpad 拖图；Quick Tools 空态；`%LOCALAPPDATA%/FireflyAI` 目录结构落位。
- **发布物**：zip + SHA256 + 简短 Release Note（引用 `docs/Release_Scope_v1.md` 的 bundled/optional external 边界）。

---

*本方案基于只读审计（启动链实读、site-packages 实测、路径解析静态核查）。未执行 git add/commit/push，未删除文件，未修改任何业务代码。下一步（实际 spec 编写与首建）需按 §9 前置项获批后进行。*
