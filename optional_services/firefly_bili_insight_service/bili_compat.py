"""Firefly_BiliInsight_Service 的依赖 shim（仅存在于服务项目，原仓库零改动）。

问题：bilichat.services.transcription 只需要 bilichat.storage.cache，
但 bilichat/storage/__init__.py 无条件执行 `from .vector import ...`，
vector.py 在模块顶层 `import chromadb`。本服务的 transcribe 路径
完全不使用向量存储，chromadb 属于无关重依赖。

方案：在导入 bilichat 之前向 sys.modules 注册一个最小 chromadb 惰性
stub（PEP 562），使 vector.py 可被导入但永远不实例化；任何试图真正
使用向量存储的调用都会得到明确报错。
"""

import sys
import types

_STUBBED = "chromadb (stubbed by Firefly_BiliInsight_Service: vector store unused)"


class _StubSettings:
    def __init__(self, *args, **kwargs):
        pass


def _unavailable(*args, **kwargs):
    raise RuntimeError(_STUBBED)


def install_shims() -> list[str]:
    """注册 chromadb stub，返回已安装的 shim 名单（幂等）。"""
    installed = []
    if "chromadb" not in sys.modules:
        chroma = types.ModuleType("chromadb")
        config = types.ModuleType("chromadb.config")
        config.Settings = _StubSettings
        chroma.config = config
        chroma.PersistentClient = _unavailable
        chroma.__getattr__ = lambda name: (_ for _ in ()).throw(
            AttributeError(f"chromadb.{name}: {_STUBBED}"))
        sys.modules["chromadb"] = chroma
        sys.modules["chromadb.config"] = config
        installed.append("chromadb")
    return installed
