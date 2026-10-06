# -*- coding: utf-8 -*-
"""AttachmentResolver — 教材附件解析器（M2.2）。

相对引用 → 安全校验 → 类型推导 → 存在性/大小检查 → 内容哈希 → 附件记录。

安全规则（全部拒绝为失败结果, 顺序即决策顺序）:
1. 空引用                        → empty_reference
2. 绝对路径逃逸（盘符//~/UNC）    → absolute_path_not_allowed
3. ".." 路径穿越（含混用分隔符）  → path_traversal
4. 解析后逃出 base_dir（兜底）    → path_traversal
5. 后缀不在白名单                → unsupported_type
6. 资源不存在/不是文件           → attachment_missing
7. 超过大小上限                  → file_too_large
8. 哈希阶段 IO 故障              → io_error

纯只读: 仅 stat/read 原文件用于校验与哈希, **不写任何路径**。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.learning.document.schema import (
    ATTACHMENT_FORMULA_ASSET,
    ATTACHMENT_IMAGE,
    DEFAULT_MAX_FILE_BYTES,
    ERR_ABSOLUTE,
    ERR_EMPTY_REF,
    ERR_IO,
    ERR_MISSING,
    ERR_TOO_LARGE,
    ERR_TRAVERSAL,
    ERR_UNSUPPORTED,
    SUPPORTED_EXTENSIONS,
    AttachmentResolution,
    DocumentAttachment,
)

_DRIVE_RE = __import__("re").compile(r"^[A-Za-z]:[\\/]")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_attachment_type(ref: str) -> str | None:
    """按后缀推导附件类型; 不在白名单返回 None。"""
    suffix = Path(ref).suffix.lower()
    for att_type, extensions in SUPPORTED_EXTENSIONS.items():
        if suffix in extensions:
            return att_type
    return None


class AttachmentResolver:
    """教材目录内相对引用 → 附件记录（错误即结果, 纯只读）。"""

    def __init__(
        self,
        base_dir: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ):
        self._base_dir = Path(base_dir)
        self._max_file_bytes = max(int(max_file_bytes), 1)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def resolve(
        self,
        ref: str,
        *,
        type: str | None = None,       # noqa: A002 - 任务规约字段名
        caption: str = "",
    ) -> AttachmentResolution:
        """解析一条附件引用; 任何问题返回失败结果, 不抛异常。"""
        # ---- 1. 空引用 -----------------------------------------------------
        reference = str(ref or "").strip()
        if not reference:
            return self._fail(ERR_EMPTY_REF, "reference is empty")

        # ---- 2. 绝对路径逃逸 -------------------------------------------------
        normalized = reference.replace("\\", "/")
        if (
            _DRIVE_RE.match(reference)
            or normalized.startswith("/")
            or reference.startswith("~")
            or Path(reference).is_absolute()
        ):
            return self._fail(ERR_ABSOLUTE, reference)

        # ---- 3. ".." 路径穿越 ------------------------------------------------
        parts = [part for part in normalized.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            return self._fail(ERR_TRAVERSAL, reference)

        # ---- 4. 类型推导与白名单 ---------------------------------------------
        att_type = type if type is not None else infer_attachment_type(reference)
        if att_type is None or att_type not in SUPPORTED_EXTENSIONS:
            return self._fail(ERR_UNSUPPORTED, reference)
        suffix = Path(reference).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS[att_type]:
            return self._fail(ERR_UNSUPPORTED, f"{reference}（类型 {att_type}）")

        # ---- 5. 解析 + 目录逃逸兜底 -------------------------------------------
        target = self._base_dir.joinpath(*parts)
        try:
            base_real = self._base_dir.resolve()
            target_real = target.resolve()
        except OSError as exc:
            return self._fail(ERR_IO, str(exc))
        if not target_real.is_relative_to(base_real):
            return self._fail(ERR_TRAVERSAL, reference)

        # ---- 6. 存在性 --------------------------------------------------------
        if not target_real.exists() or not target_real.is_file():
            return self._fail(ERR_MISSING, reference)

        # ---- 7. 大小上限 -------------------------------------------------------
        try:
            size = target_real.stat().st_size
        except OSError as exc:
            return self._fail(ERR_IO, str(exc))
        if size > self._max_file_bytes:
            return self._fail(
                ERR_TOO_LARGE,
                f"{reference}（{size} bytes > 上限 {self._max_file_bytes}）",
            )

        # ---- 8. 内容哈希 --------------------------------------------------------
        try:
            content_hash = _sha256_file(target_real)
        except OSError as exc:
            return self._fail(ERR_IO, str(exc))

        attachment_id = "att-" + hashlib.sha1(
            f"{att_type}|{normalized}|{content_hash}".encode("utf-8")
        ).hexdigest()[:12]

        return AttachmentResolution(
            ok=True,
            attachment=DocumentAttachment(
                attachment_id=attachment_id,
                type=att_type,
                source_path=normalized,
                stored_path=str(target_real),
                hash=content_hash,
                metadata={
                    "bytes": size,
                    "extension": suffix,
                    "caption": caption,
                    "base_dir": str(base_real),
                },
            ),
        )

    @staticmethod
    def _fail(error: str, detail: str) -> AttachmentResolution:
        return AttachmentResolution(ok=False, error=error, detail=detail)
