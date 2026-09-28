# -*- coding: utf-8 -*-
"""宿主侧视觉预留接口（vision 插件未安装时的降级门面）.

视觉引擎实现已迁移至插件 ``plugins/firefly_vision/screen_vision``。
本模块只做转发：

- vision 插件已安装（能力层判定）→ 属性/子模块全部转发到插件实现；
- 未安装 → 任何访问抛 CapabilityMissingError（宿主入口据此在
  界面顶部提示「暂未安装此插件」），绝不静默降级为"可用"。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 子模块解析顺序：宿主自有数据契约（models.py 等本目录文件）优先，
# 其余（config/service/vision/brain…）转发到插件实现目录。
_SELF = Path(__file__).resolve().parent
_IMPL = _SELF.parent.parent / "plugins" / "firefly_vision" / "screen_vision"
__path__ = [str(_SELF)] + ([str(_IMPL)] if _IMPL.is_dir() else [])
# 插件实现包的内部导入（from screen_vision.xxx import …）需要它的根目录
# 在 sys.path 上——必须在任何子模块被导入前注册（app 启动早期的模块级
# 导入就会触达这里）。
_IMPL_ROOT = str(_IMPL.parent)
if _IMPL.is_dir() and _IMPL_ROOT not in sys.path:
    sys.path.insert(0, _IMPL_ROOT)


def _available() -> bool:
    from core.capabilities import is_available

    return is_available("vision")


def _missing(exc_factory=None):
    from core.capabilities import CapabilityMissingError, missing_message

    raise CapabilityMissingError("vision", missing_message("vision"))


def __getattr__(name: str):
    # 转发策略：实现可导入即转发（能力门控在宿主功能入口）；
    # 实现不存在（经典版未装插件）才抛缺失提示。
    try:
        import importlib

        impl = importlib.import_module("screen_vision")
    except ImportError as exc:
        from core.capabilities import CapabilityMissingError, missing_message

        raise CapabilityMissingError("vision", missing_message("vision")) from exc
    try:
        return getattr(impl, name)
    except AttributeError as exc:
        raise AttributeError(f"screen_vision has no attribute {name!r}") from exc
