# -*- coding: utf-8 -*-
"""DocumentPackage — 教材目录级资源管理（M2.2）。

一个教材目录 = 一个包: 持有 ``AttachmentResolver``, 收集整本教材解析出的
附件引用, 产出通过/失败的完整清单与统计。与 M2.1 提取器兼容——

    NativeExtractor.extract(md) ──image refs──▶ DocumentPackage.from_extracted_document
                                                 ──▶ 逐引用解析（含 caption）+ 统计

纯内存收集 + 只读文件访问; **不复制、不移动、不修改原始教材任何文件**。
"""

from __future__ import annotations

from pathlib import Path

from core.learning.document.resolver import AttachmentResolver
from core.learning.document.schema import (
    DEFAULT_MAX_FILE_BYTES,
    AttachmentResolution,
    DocumentAttachment,
)


class DocumentPackage:
    """教材包: 目录级附件登记簿（只读收集, 零写入）。"""

    def __init__(
        self,
        base_dir: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ):
        self._base_dir = Path(base_dir)
        self._resolver = AttachmentResolver(
            self._base_dir, max_file_bytes=max_file_bytes
        )
        self._attachments: list[DocumentAttachment] = []
        self._failed: list[AttachmentResolution] = []

    # ------------------------------------------------------------------
    # 解析与登记
    # ------------------------------------------------------------------

    def resolve(
        self,
        ref: str,
        *,
        type: str | None = None,       # noqa: A002 - 任务规约字段名
        caption: str = "",
        register: bool = True,
    ) -> AttachmentResolution:
        """解析一条引用; 成功默认登记进包（register=False 仅试解析）。"""
        resolution = self._resolver.resolve(ref, type=type, caption=caption)
        if resolution.ok and register:
            self._attachments.append(resolution.attachment)
        elif not resolution.ok:
            self._failed.append(resolution)
        return resolution

    def resolve_all(
        self,
        refs: list[str] | tuple[str, ...],
        *,
        captions: dict[str, str] | None = None,
    ) -> tuple[AttachmentResolution, ...]:
        """批量解析（重复引用会去重登记: 同 id 只保留一条）。"""
        captions = captions or {}
        resolutions = tuple(
            self.resolve(ref, caption=captions.get(ref, "")) for ref in refs
        )
        self._dedupe()
        return resolutions

    def collect_from_extracted(self, extracted) -> tuple[AttachmentResolution, ...]:
        """从 M2.1 ``ExtractedDocument`` 收集全部图片引用（含 caption）。

        只读消费（鸭子类型: 有 ``.images`` 即可）, 不 import extractor。
        """
        captions = {img.path: img.caption for img in extracted.images}
        refs = list(dict.fromkeys(img.path for img in extracted.images))
        return self.resolve_all(refs, captions=captions)

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def attachments(self) -> tuple[DocumentAttachment, ...]:
        return tuple(self._attachments)

    @property
    def failed(self) -> tuple[AttachmentResolution, ...]:
        return tuple(self._failed)

    def stats(self) -> dict:
        by_error: dict[str, int] = {}
        for resolution in self._failed:
            by_error[resolution.error] = by_error.get(resolution.error, 0) + 1
        return {
            "resolved": len(self._attachments),
            "failed": len(self._failed),
            "by_type": {
                att_type: sum(1 for a in self._attachments if a.type == att_type)
                for att_type in {a.type for a in self._attachments}
            },
            "errors": by_error,
        }

    def to_dict(self) -> dict:
        return {
            "base_dir": str(self._base_dir),
            "attachments": [a.to_dict() for a in self._attachments],
            "failed": [f.to_dict() for f in self._failed],
            "stats": self.stats(),
        }

    def to_json(self, indent: int = 2) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _dedupe(self) -> None:
        seen: set[str] = set()
        unique: list[DocumentAttachment] = []
        for attachment in self._attachments:
            if attachment.attachment_id in seen:
                continue
            seen.add(attachment.attachment_id)
            unique.append(attachment)
        self._attachments = unique
