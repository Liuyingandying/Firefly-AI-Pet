# -*- coding: utf-8 -*-
"""一次性：从同一构建分装 classic / plus 双发行 zip。用后即删。"""
import os
import shutil
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC = REPO / "dist" / "Firefly_AI_Pet_rc2"
OUT = Path(r"C:\Users\LENOVO\Desktop\流萤")

NOTES = """【Plus 版插件说明】

本包 plugins\\ 目录内置全部能力/适配器插件，解压即用：
- firefly_vision        视觉（屏幕视觉/相机/图片理解，TJU→DeepSeek→GLM 故障转移）
- firefly_learning      学习模式
- firefly_voice_chat    语音对话（麦克风 STT + 回复朗读）
- firefly_bili_video    B站视频解析
- firefly_voice         语音服务状态管理
- firefly_camera_vision / firefly_video_extension / learning_focus / tju_info_retrieval

【两个需要额外初始化的重能力】
1. 语音对话（TTS+RVC）：首次使用需按 plugins\\firefly_voice\\voice_module\\README.md
   部署 Python 3.10 venv + torch + 模型权重（详见其 models/manifest.json），
   然后在 Voice Settings 面板启动语音服务。
2. B站视频解析：需在 plugins\\firefly_bili_insight_service（如发行版附带）
   或按其 README 部署 venv 与 ffmpeg 后使用。

未安装对应插件时，宿主功能入口会提示「暂未安装此插件」，不影响其他功能。
"""


def copy_tree(src: Path, dst: Path, skip_dirs=("plugins",)):
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        if any(part in skip_dirs for part in rel.parts):
            dirs[:] = []
            continue
        for d in list(dirs):
            if d in skip_dirs:
                dirs.remove(d)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(Path(root) / f, dst / rel / f)


def make_zip(pkg_root: Path, zip_path: Path, inner: str):
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in sorted(pkg_root.rglob("*")):
            if f.is_file():
                zf.write(f, f"{inner}/{f.relative_to(pkg_root).as_posix()}")
                count += 1
    return count


# ---- Classic（无插件）----
classic = REPO / "_pkg_classic"
if classic.exists():
    shutil.rmtree(classic)
copy_tree(SRC, classic)
(note := classic / "README-经典版.txt").write_text(
    "经典版：不含能力插件。视觉/学习模式/语音对话/B站视频解析等能力\n"
    "需要安装对应插件后使用（将插件放入 exe 同级 plugins\\ 目录）。\n"
    "请搭配 Firefly_AI_Pet_v1.0-rc4_plus_win64.zip 获取全部插件。\n",
    encoding="utf-8",
)
zc = OUT / "Firefly_AI_Pet_v1.0-rc4_classic_win64.zip"
if zc.exists():
    zc.unlink()
print("classic files:", make_zip(classic, zc, "Firefly_AI_Pet"), "->", zc.name)

# ---- Plus（全部插件）----
plus = REPO / "_pkg_plus"
if plus.exists():
    shutil.rmtree(plus)
copy_tree(SRC, plus, skip_dirs=())
(plus / "plugins").mkdir(exist_ok=True)
(plus / "plugins" / "插件说明.txt").write_text(NOTES, encoding="utf-8")
zp = OUT / "Firefly_AI_Pet_v1.0-rc4_plus_win64.zip"
if zp.exists():
    zp.unlink()
print("plus files:", make_zip(plus, zp, "Firefly_AI_Pet"), "->", zp.name)
print("BOTH_PACKAGES_OK")
