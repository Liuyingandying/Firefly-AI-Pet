# -*- coding: utf-8 -*-
"""一次性：宿主残余 screen_vision 直导 → 宿主契约/懒加载降级。用后即删。"""
from pathlib import Path

FILES = [
    "core/video_vision.py",
    "ui/pdf_selection_overlay.py",
    "ui/companion_attachment.py",
    "ui/pdf_visual_worker.py",
]
for f in FILES:
    p = Path(f)
    src = p.read_text(encoding="utf-8")
    # models 契约 → 宿主副本（core.screen_vision.models 经 __path__ 也指向实现目录，
    # 但 models.py 已复制到宿主目录，稳定可用）
    src = src.replace(
        "from core.screen_vision.models import",
        "from core.screen_vision.models import",
    )
    p.write_text(src, encoding="utf-8")
    print("models fixed:", f)

# video_vision.py 的 vision.base → 兼容导入
p = Path("core/video_vision.py")
src = p.read_text(encoding="utf-8")
old = "from screen_vision.vision.base import VisionProvider"
new = (
    "try:" + NL
    + "    from screen_vision.vision.base import VisionProvider" + NL
    + "except ImportError:  # vision 插件未安装" + NL
    + "    VisionProvider = object"
)
assert old in src
p.write_text(src.replace(old, new, 1), encoding="utf-8")
print("video_vision base guarded")
