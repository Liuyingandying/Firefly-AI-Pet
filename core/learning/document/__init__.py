# -*- coding: utf-8 -*-
"""Firefly Learning Mode M2.2 — Document Attachment Layer（教材附件管理层）。

纯文件层: 相对引用解析/路径安全/哈希/目录级登记;
错误即结果, JSON 可序列化, 不复制不修改原始教材;
不接 LLM/OCR/PDF, 不修改 DocumentExtractor/CurriculumDraft/KnowledgeGraph/UI。

注: 与 UI 层 ``ui.companion_attachment.DocumentAttachment``（聊天附件）
同名共存——本包是教材资产记录, 字段集与语义完全不同, 互不引用。
"""

from core.learning.document.attachment import DocumentPackage
from core.learning.document.resolver import AttachmentResolver, infer_attachment_type
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

__all__ = [
    "DocumentPackage",
    "AttachmentResolver",
    "infer_attachment_type",
    "DocumentAttachment",
    "AttachmentResolution",
    "ATTACHMENT_IMAGE",
    "ATTACHMENT_FORMULA_ASSET",
    "SUPPORTED_EXTENSIONS",
    "DEFAULT_MAX_FILE_BYTES",
    "ERR_EMPTY_REF",
    "ERR_ABSOLUTE",
    "ERR_TRAVERSAL",
    "ERR_UNSUPPORTED",
    "ERR_MISSING",
    "ERR_TOO_LARGE",
    "ERR_IO",
]
