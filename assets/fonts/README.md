# assets/fonts/ — 应用级字体资源（科研级渲染）

本目录的 `.ttf` / `.otf` 字体文件会在应用启动时通过
`QFontDatabase.addApplicationFont()` 自动注册（`ui/theme.py::load_application_fonts`，
幂等；缺失或损坏的文件静默跳过，不影响启动）。

## 推荐放置（科研级数学渲染）

| 文件 | 字体族 | 用途 |
|---|---|---|
| `STIXTwoMath-Regular.ttf` 等 | STIX Two Math | 数学/物理公式优先渲染 |
| `NotoSansCJKsc-*.otf` | Noto Sans CJK SC | 中日韩与全角符号兜底 |
| `DejaVuSans.ttf` | DejaVu Sans | 技术字母/扩展符号兜底 |
| `Symbola.ttf` | Symbola | 罕用符号最后兜底 |

> 请自行下载并遵守各字体的开源许可（STIX/OFL、Noto/OFL、DejaVu/free 均可再分发）。
> 本仓库**不附带**任何字体文件。

## 未放置时的行为

字体栈（`theme.V2.FONT_STACK_FAMILIES`）仍然生效——系统已安装的同名字体
（如 Windows 自带 Cambria Math / Microsoft YaHei UI / Segoe UI）会正常兜底；
仅 STIX/Symbola 等非系统字体不可用。
