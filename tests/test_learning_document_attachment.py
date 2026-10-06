# -*- coding: utf-8 -*-
"""M2.2 Document Attachment Layer 验收测试。

覆盖：图片解析 / hash 一致 / 路径穿越拒绝 / 缺失附件 / JSON 序列化 /
M2.1 extractor 兼容, 另加绝对路径逃逸、不支持类型、超大文件、
formula_asset、纯只读契约（教材零改动）。
隔离：全部在 tmp 目录; 纯文件层, 不接 LLM/OCR/PDF, 不触课程存储/UI。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from core.learning.document import (
    ATTACHMENT_FORMULA_ASSET,
    ATTACHMENT_IMAGE,
    ERR_ABSOLUTE,
    ERR_EMPTY_REF,
    ERR_MISSING,
    ERR_TOO_LARGE,
    ERR_TRAVERSAL,
    ERR_UNSUPPORTED,
    AttachmentResolver,
    DocumentAttachment,
    DocumentPackage,
)
from core.learning.extractor import NativeExtractor

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-payload-0123456789"


@pytest.fixture(scope="module")
def textbook_dir(tmp_path_factory):
    """一个微型教材目录: figs/ 图片 + eqs/ 公式源 + 秘密文件（穿越目标）。"""
    base = tmp_path_factory.mktemp("textbook")
    (base / "figs").mkdir()
    (base / "eqs").mkdir()
    (base / "figs" / "wave.png").write_bytes(PNG_BYTES)
    (base / "figs" / "antenna.jpg").write_bytes(b"\xff\xd8fake-jpeg")
    (base / "eqs" / "maxwell.tex").write_text("\\nabla \\times E = -\\partial B/\\partial t", encoding="utf-8")
    (base / "secret.txt").write_text("top secret", encoding="utf-8")
    (base / "textbook.md").write_text(
        "# 章\n\n![平面波](figs/wave.png)\n\n![缺失图](figs/ghost.png)\n",
        encoding="utf-8",
    )
    return base


@pytest.fixture(scope="module")
def resolver(textbook_dir):
    return AttachmentResolver(textbook_dir)


# ---------------------------------------------------------------------------
# 1. 图片解析
# ---------------------------------------------------------------------------

def test_image_resolution(resolver, textbook_dir):
    res = resolver.resolve("figs/wave.png", caption="平面波")
    assert res.ok is True
    att = res.attachment
    assert isinstance(att, DocumentAttachment)
    assert att.type == ATTACHMENT_IMAGE
    assert att.source_path == "figs/wave.png"
    att_path = Path(att.stored_path)
    assert att_path.is_absolute() and att_path.is_file()
    assert att_path.resolve().is_relative_to(textbook_dir.resolve())   # 不逃出教材目录
    assert att.metadata["caption"] == "平面波"
    assert att.metadata["extension"] == ".png"
    assert att.metadata["bytes"] == len(PNG_BYTES)


def test_image_forward_and_backslash_refs(resolver):
    """混用分隔符 / 反斜杠引用都归一解析。"""
    assert resolver.resolve("figs/wave.png").ok is True
    assert resolver.resolve("figs\\wave.png").ok is True


# ---------------------------------------------------------------------------
# 2. hash 一致
# ---------------------------------------------------------------------------

def test_hash_consistency_and_deterministic_id(resolver):
    expected = hashlib.sha256(PNG_BYTES).hexdigest()
    r1 = resolver.resolve("figs/wave.png")
    r2 = resolver.resolve("figs/wave.png")
    assert r1.attachment.hash == r2.attachment.hash == expected
    assert r1.attachment.attachment_id == r2.attachment.attachment_id  # 确定性 id


def test_same_content_different_ref_same_hash(resolver, textbook_dir):
    copy = textbook_dir / "figs" / "wave_copy.png"
    copy.write_bytes(PNG_BYTES)
    try:
        r1 = resolver.resolve("figs/wave.png")
        r2 = resolver.resolve("figs/wave_copy.png")
        assert r1.attachment.hash == r2.attachment.hash          # 内容哈希一致
        assert r1.attachment.attachment_id != r2.attachment.attachment_id  # ref 不同 → id 不同
    finally:
        copy.unlink()                                            # 清理测试自建文件


def test_formula_asset_type(resolver, textbook_dir):
    res = resolver.resolve("eqs/maxwell.tex")
    assert res.ok is True
    assert res.attachment.type == ATTACHMENT_FORMULA_ASSET
    assert "\\nabla" in (textbook_dir / "eqs" / "maxwell.tex").read_text(encoding="utf-8")


def test_explicit_type_mismatch_rejected(resolver):
    res = resolver.resolve("eqs/maxwell.tex", type=ATTACHMENT_IMAGE)
    assert res.ok is False and res.error == ERR_UNSUPPORTED


# ---------------------------------------------------------------------------
# 3. 路径穿越拒绝
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref", [
    "../secret.txt",
    "figs/../../secret.txt",
    "figs\\..\\..\\secret.txt",
    "..%2Fsecret.txt".replace("%2F", "/"),
])
def test_traversal_rejected(resolver, ref):
    res = resolver.resolve(ref)
    assert res.ok is False
    assert res.error == ERR_TRAVERSAL


def test_absolute_path_escape_rejected(resolver):
    for ref in ("C:\\Windows\\notepad.exe", "/etc/passwd", "~/.ssh/id_rsa"):
        res = resolver.resolve(ref)
        assert res.ok is False
        assert res.error == ERR_ABSOLUTE


def test_unsupported_type_rejected(resolver, textbook_dir):
    script = textbook_dir / "figs" / "evil.exe"
    script.write_bytes(b"MZ...")
    try:
        res = resolver.resolve("figs/evil.exe")
        assert res.ok is False and res.error == ERR_UNSUPPORTED
    finally:
        script.unlink()


# ---------------------------------------------------------------------------
# 4. 缺失附件
# ---------------------------------------------------------------------------

def test_missing_attachment(resolver):
    res = resolver.resolve("figs/ghost.png")
    assert res.ok is False
    assert res.error == ERR_MISSING
    assert res.attachment is None


def test_empty_reference_rejected(resolver):
    assert resolver.resolve("").error == ERR_EMPTY_REF
    assert resolver.resolve("   ").error == ERR_EMPTY_REF
    assert resolver.resolve(None).error == ERR_EMPTY_REF


# ---------------------------------------------------------------------------
# 5. 超大文件
# ---------------------------------------------------------------------------

def test_oversized_file_rejected(textbook_dir):
    big = textbook_dir / "figs" / "huge.png"
    big.write_bytes(b"x" * 128)
    try:
        strict = AttachmentResolver(textbook_dir, max_file_bytes=64)
        res = strict.resolve("figs/huge.png")
        assert res.ok is False and res.error == ERR_TOO_LARGE
        assert "128" in res.detail
        # 默认上限下同一文件正常通过
        assert AttachmentResolver(textbook_dir).resolve("figs/huge.png").ok is True
    finally:
        big.unlink()


# ---------------------------------------------------------------------------
# 6. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(resolver):
    res = resolver.resolve("figs/wave.png", caption="平面波")
    d = res.attachment.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["type"] == "image"
    assert parsed["hash"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert parsed["metadata"]["caption"] == "平面波"

    failed = resolver.resolve("../secret.txt")
    parsed_fail = json.loads(failed.to_json())
    assert parsed_fail["ok"] is False and parsed_fail["attachment"] is None
    assert parsed_fail["error"] == ERR_TRAVERSAL


# ---------------------------------------------------------------------------
# 7. DocumentPackage: 教材目录级管理
# ---------------------------------------------------------------------------

def test_package_collect_and_stats(textbook_dir):
    package = DocumentPackage(textbook_dir)
    resolutions = package.resolve_all(
        ["figs/wave.png", "figs/antenna.jpg", "figs/wave.png", "figs/ghost.png"]
    )
    assert [r.ok for r in resolutions] == [True, True, True, False]
    assert len(package.attachments) == 2                    # 重复引用去重登记
    assert len(package.failed) == 1
    stats = package.stats()
    assert stats["resolved"] == 2 and stats["failed"] == 1
    assert stats["by_type"].get(ATTACHMENT_IMAGE) == 2
    assert stats["errors"].get(ERR_MISSING) == 1
    # 包 JSON 可序列化
    parsed = json.loads(package.to_json())
    assert parsed["stats"]["resolved"] == 2


def test_package_custom_size_limit(textbook_dir):
    package = DocumentPackage(textbook_dir, max_file_bytes=8)
    res = package.resolve("figs/wave.png")
    assert res.ok is False and res.error == ERR_TOO_LARGE
    assert package.stats()["errors"].get(ERR_TOO_LARGE) == 1


# ---------------------------------------------------------------------------
# 8. M2.1 extractor 兼容 + 纯只读契约
# ---------------------------------------------------------------------------

def test_m21_extractor_compatibility(textbook_dir):
    """extractor 的图片引用 → DocumentPackage 逐条解析（caption 带过来）。"""
    result = NativeExtractor().extract(textbook_dir / "textbook.md")
    assert result.success is True

    package = DocumentPackage(textbook_dir)
    resolutions = package.collect_from_extracted(result.document_structure)

    assert len(resolutions) == 2
    ok = [r for r in resolutions if r.ok]
    assert len(ok) == 1
    assert ok[0].attachment.metadata["caption"] == "平面波"
    assert ok[0].attachment.source_path == "figs/wave.png"
    assert package.failed[0].error == ERR_MISSING
    assert package.failed[0].detail == "figs/ghost.png"


def test_textbook_never_modified(textbook_dir):
    """纯只读契约：解析前后, 教材文件的内容哈希与 mtime 完全不变。"""
    tracked = [
        textbook_dir / "textbook.md",
        textbook_dir / "figs" / "wave.png",
        textbook_dir / "eqs" / "maxwell.tex",
        textbook_dir / "secret.txt",
    ]
    before = {
        p: (hashlib.sha256(p.read_bytes()).hexdigest(), os.stat(p).st_mtime)
        for p in tracked
    }

    package = DocumentPackage(textbook_dir)
    package.resolve_all(
        ["figs/wave.png", "eqs/maxwell.tex", "../secret.txt", "figs/ghost.png"]
    )

    after = {
        p: (hashlib.sha256(p.read_bytes()).hexdigest(), os.stat(p).st_mtime)
        for p in tracked
    }
    assert before == after
