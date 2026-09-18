# Firefly AI Pet — Windows Packaging (v1.0-rc1 dry build)

本目录是可重复的 Windows 发布构建链。方案依据：`docs/Packaging_Plan_v1.md`（PyInstaller onedir + 便携 zip）。

## 文件

| 文件 | 作用 |
|---|---|
| `firefly.spec` | PyInstaller onedir 规格：数据白名单、hiddenimports、可选链 excludes |
| `build_windows.ps1` | 一键构建：环境检查 → PyInstaller 安装/检查 → 构建 → 后置拷贝 → `build_report.json` |
| `verify_dist.py` | 对 `dist/Firefly_AI_Pet/` 的静态验收（不启动应用）：入口/资源/配置模板/禁运扫描，输出 PASS/FAIL |

## 用法（仓库根目录）

```powershell
# dry build（当前阶段，不发布、不生成 installer）
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

# 验收
.venv\Scripts\python.exe packaging\verify_dist.py
```

可选：`build_windows.ps1 -CleanBuild` 会先移除 `build\`、`dist\`（默认关闭；默认模式仅靠 `--noconfirm` 覆盖，不做删除）。

## 布局说明（重要）

PyInstaller 6.x onedir 将**全部数据**放入 `_internal/`。这与源码中 `Path(__file__)`
定位（`app.py` / `core/settings_manager.py` / `providers/base.py` / `voice_client/config.py`）
在冻结后解析到 `_internal` 的事实**完全一致**——零业务代码改动即可运行。
因此实际布局为：

```
dist/Firefly_AI_Pet/
├── Firefly_AI_Pet.exe
├── extensions/pagelens_bridge/   # 后置拷贝，供用户装入浏览器
├── .env.example                  # 后置拷贝
└── _internal/
    ├── assets/  (animations/ firefly.ico paper_reader/)
    ├── character/firefly/ (persona yaml)
    ├── config/  (仅 4 个模板：companion.json / voice_config.yaml /
    │             path_config.yaml / hardware_devices.example.json)
    └── (python runtime, Qt, onnxruntime+rapidocr, PyMuPDF, qdrant/fastembed/mem0 …)
```

顶层平铺 assets/config 的展示式布局需要 `app_root()` 路径垫片（P1 工程改动，本阶段不采用）。

## 红线（构建与验收都强制执行）

- `config/` 只收 4 个**模板**；本地用户文件（`hardware_devices.json`、
  `pet_preferences.json`、`ui_settings.json`、`sessions.json`）绝不入包。
- 不打包 `runtime/`、logs、备份、`*.sqlite/*.db`、`.env`、任何用户数据。
- 本地视频可选链（faster-whisper / ctranslate2 / scenedetect）不随包（Optional External，按既有降级路径）。

## 当前状态

**dry build 阶段**：只构建与验收，不签名、不做 installer、不上传、不发布。
产物报告：`packaging/build_report.json`；构建记录：`docs/Packaging_Build_First_Report.md`。
