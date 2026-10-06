# -*- coding: utf-8 -*-
"""Document attachment schema — 教材附件层协议（M2.2, M2.0 §4.2 落地）。

设计约束:
- **纯文件层**: 只读教材目录, 不复制/不修改原始教材（stored_path = 解析后的
  绝对路径, 非拷贝目标）; 无 LLM/无 OCR/无 UI;
- 词表封闭: 附件类型 / 支持后缀 / 失败码各只有一份定义;
- 错误即结果: ``AttachmentResolution`` 承载成功与失败, 不抛异常穿越层边界;
- 全字段 JSON 原生类型。

与 UI 层 ``ui.companion_attachment.DocumentAttachment``（聊天附件, 禁改）
同名共存: 字段集与语义完全不同（教材资产记录 vs 聊天字节附件）, 互不引用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 附件类型词表（封闭）
# ---------------------------------------------------------------------------

ATTACHMENT_IMAGE = "image"                    # 图片资产（png/jpg/gif/svg/…）
ATTACHMENT_FORMULA_ASSET = "formula_asset"    # 外部公式源（.tex/.latex）

#: 支持的文件后缀（按类型; 大小写不敏感）
SUPPORTED_EXTENSIONS: dict[str, frozenset[str]] = {
    ATTACHMENT_IMAGE: frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}),
    ATTACHMENT_FORMULA_ASSET: frozenset({".tex", ".latex"}),
}

#: 全部受支持后缀（类型推导用）
_ALL_SUPPORTED: frozenset[str] = frozenset().union(*SUPPORTED_EXTENSIONS.values())

#: 默认单文件大小上限（字节; 可构造注入覆盖）
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024

# ---------------------------------------------------------------------------
# 失败码封闭词表（error 字段, 机器可读）
# ---------------------------------------------------------------------------

ERR_EMPTY_REF = "empty_reference"             # 引用为空
ERR_ABSOLUTE = "absolute_path_not_allowed"    # 绝对路径逃逸（盘符//~/UNC）
ERR_TRAVERSAL = "path_traversal"              # ".." 路径穿越
ERR_UNSUPPORTED = "unsupported_type"          # 后缀不在白名单
ERR_MISSING = "attachment_missing"            # 资源不存在/不是文件
ERR_TOO_LARGE = "file_too_large"              # 超过大小上限
ERR_IO = "io_error"                           # 读取故障（哈希阶段）


@dataclass(frozen=True)
class DocumentAttachment:
    """一条教材附件记录（只读资产, stored_path 指向原文件, 不拷贝）。"""

    attachment_id: str                  # 确定性 id: type|ref|content_hash 派生
    type: str                           # ATTACHMENT_IMAGE / ATTACHMENT_FORMULA_ASSET
    source_path: str                    # 教材内引用的相对路径（原样）
    stored_path: str                    # 解析后的绝对路径（指向原文件）
    hash: str                           # 内容 sha256（hex）
    metadata: dict = field(default_factory=dict)   # bytes/extension/caption/…

    def to_dict(self) -> dict:
        return {
            "attachment_id": self.attachment_id,
            "type": self.type,
            "source_path": self.source_path,
            "stored_path": self.stored_path,
            "hash": self.hash,
            "metadata": dict(self.metadata),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class AttachmentResolution:
    """一次解析的完整结果（成功与失败都是结果, 不抛异常）。"""

    ok: bool
    attachment: DocumentAttachment | None = None
    error: str = ""                     # ok=True 为 ""; 否则为失败码
    detail: str = ""                    # 失败详情（路径/上限值等）

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "attachment": self.attachment.to_dict() if self.attachment is not None else None,
            "error": self.error,
            "detail": self.detail,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
