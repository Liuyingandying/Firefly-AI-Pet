# -*- coding: utf-8 -*-
"""DocumentExtractor 协议 + 注册表（M2.1, M2.0 §3 落地）。

协议要点（M2.0 收紧版）:
- ``can_extract`` 只读无副作用（格式后缀 + 依赖可用）, 不得抛异常;
- ``extract`` 永不抛出——失败返回 ``DocumentExtractionResult(success=False,
  error="<错误码>: <详情>")``;
- ``ExtractorRegistry`` 按 priority 升序取第一个 ``can_extract`` 为真者,
  全缺返回 None（调用方降级到手动导入/PageLens 阅读链路）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from core.learning.extractor.schema import DocumentExtractionResult

__all__ = [
    "DocumentExtractor",
    "ExtractorRegistry",
]


@runtime_checkable
class DocumentExtractor(Protocol):
    """教材提取协议（结构化鸭子协议, 不绑定实现）。"""

    name: str

    def can_extract(self, source: Path) -> bool:
        """能力探测: 格式支持 + 依赖可用。只读, 无副作用。"""
        ...  # pragma: no cover

    def extract(self, source: Path) -> DocumentExtractionResult:
        """提取。永不抛出; 失败返回 error 非空的失败结果。"""
        ...  # pragma: no cover


class ExtractorRegistry:
    """优先级注册表（priority 数值越小越先被 resolve）。"""

    def __init__(self) -> None:
        self._extractors: list[tuple[int, DocumentExtractor]] = []

    def register(self, extractor: DocumentExtractor, priority: int) -> None:
        if priority < 0:
            raise ValueError(f"priority must be >= 0 (got {priority})")
        self._extractors.append((priority, extractor))
        self._extractors.sort(key=lambda pair: pair[0])

    def resolve(self, source: Path) -> DocumentExtractor | None:
        """按优先级取第一个 can_extract() 为真的实现; 全缺 → None。"""
        for _, extractor in self._extractors:
            try:
                if extractor.can_extract(Path(source)):
                    return extractor
            except Exception:  # noqa: BLE001 - 探测故障视作不支持, 不断链
                continue
        return None

    def registered_names(self) -> tuple[str, ...]:
        return tuple(extractor.name for _, extractor in self._extractors)
