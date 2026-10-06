# -*- coding: utf-8 -*-
"""M2.1 Native Document Extractor 验收测试。

覆盖：markdown 章节解析 / formula 解析 / image 解析 / 空文件 /
非法路径 / JSON 序列化 / 与已有 DocumentStructure validator 兼容,
另加注册表、能力探测与纯只读契约。
隔离：全部在 tmp 目录读写; 不接 Docling/MinerU/LLM, 不触课程存储。
"""

from __future__ import annotations

import json

import pytest

from core.learning.extractor import (
    ERR_DECODE,
    ERR_EMPTY,
    ERR_NOT_FOUND,
    ERR_NOT_SUPPORTED,
    ERR_READ,
    EXTRACTOR_NATIVE,
    DocumentExtractionResult,
    ExtractorRegistry,
    NativeExtractor,
    to_document_structure,
)


@pytest.fixture(scope="module")
def extractor():
    return NativeExtractor()


# ---------------------------------------------------------------------------
# 1. markdown 章节解析
# ---------------------------------------------------------------------------

MD_SAMPLE = """# 电磁场导论

这是一本教材的引言部分。

## 均匀平面波

平面波是最简单的电磁波形式。

$$E(z,t) = E_0 \\cos(kz - \\omega t)$$

如图所示 ![平面波示意](figs/wave.png) 展示传播方向。

### TE 极化

电场垂直于入射面，行内公式 $k = 2\\pi/\\lambda$ 表示波数。

# 天线基础

第二章内容。
"""


@pytest.fixture(scope="module")
def md_result(extractor, tmp_path_factory):
    path = tmp_path_factory.mktemp("md") / "textbook.md"
    path.write_text(MD_SAMPLE, encoding="utf-8")
    return extractor.extract(path)


def test_markdown_section_tree(md_result):
    assert md_result.success is True
    assert md_result.extractor_name == EXTRACTOR_NATIVE
    assert md_result.error == ""
    doc = md_result.document_structure
    assert doc is not None
    assert doc.title == "电磁场导论"                      # 首个 # 标题

    sections = doc.sections
    assert [s.title for s in sections] == ["电磁场导论", "均匀平面波", "TE 极化", "天线基础"]
    assert [s.level for s in sections] == [1, 2, 3, 1]
    # 树关系: level-2/3 挂正确父级
    wave, te, antenna = sections[1], sections[2], sections[3]
    assert wave.parent_id == sections[0].section_id
    assert te.parent_id == wave.section_id
    assert antenna.parent_id is None and antenna.position == 1   # 第二章
    # 兄弟位置: 两章 position 0/1, 波章下 wave 是第 0 个子节
    assert sections[0].position == 0 and antenna.position == 1
    assert wave.position == 0
    # 章节 id 唯一且稳定格式
    assert len({s.section_id for s in sections}) == 4


def test_markdown_paragraphs(md_result):
    doc = md_result.document_structure
    intro, wave = doc.sections[0], doc.sections[1]
    assert [p.text for p in intro.paragraphs] == ["这是一本教材的引言部分。"]
    assert wave.paragraphs[0].text == "平面波是最简单的电磁波形式。"
    # 图文同行: 图片提取后剩余文本仍在段落里
    assert any("展示传播方向" in p.text for p in wave.paragraphs)


def test_preamble_joins_first_section(extractor, tmp_path):
    path = tmp_path / "pre.md"
    path.write_text("开场白在第一个标题之前。\n\n# 章\n\n正文。\n", encoding="utf-8")
    result = extractor.extract(path)
    doc = result.document_structure
    assert [s.title for s in doc.sections] == ["章"]
    assert doc.sections[0].paragraphs[0].text == "开场白在第一个标题之前。"


# ---------------------------------------------------------------------------
# 2. formula 解析
# ---------------------------------------------------------------------------

def test_formula_parsing(md_result):
    doc = md_result.document_structure
    wave, te = doc.sections[1], doc.sections[2]
    assert len(wave.formulas) == 1
    f = wave.formulas[0]
    assert f.display is True
    assert f.latex == "E(z,t) = E_0 \\cos(kz - \\omega t)"
    assert f.section_id == wave.section_id
    # 行内公式: display=False, latex 去掉 $
    assert [x.latex for x in te.formulas] == ["k = 2\\pi/\\lambda"]
    assert te.formulas[0].display is False


def test_multiline_display_formula(extractor, tmp_path):
    path = tmp_path / "multiline.md"
    path.write_text("$$\n\\nabla \\times E\n= -\\frac{\\partial B}{\\partial t}\n$$\n", encoding="utf-8")
    doc = extractor.extract(path).document_structure
    assert len(doc.formulas) == 1
    assert "\\nabla \\times E" in doc.formulas[0].latex
    assert doc.formulas[0].display is True


# ---------------------------------------------------------------------------
# 3. image 解析
# ---------------------------------------------------------------------------

def test_image_parsing(md_result):
    doc = md_result.document_structure
    wave = doc.sections[1]
    assert len(wave.images) == 1
    img = wave.images[0]
    assert img.path == "figs/wave.png"                # 引用路径原样保留
    assert img.caption == "平面波示意"
    assert img.section_id == wave.section_id


def test_image_empty_ref_warns(extractor, tmp_path):
    path = tmp_path / "badimg.md"
    path.write_text("# 章\n\n![](  )\n", encoding="utf-8")
    result = extractor.extract(path)
    assert result.success is True
    assert "image reference" in result.document_structure.warnings[0]
    assert len(result.document_structure.images) == 0


# ---------------------------------------------------------------------------
# 4. 空文件 / 非法路径（错误即结果）
# ---------------------------------------------------------------------------

def test_empty_file_fails(extractor, tmp_path):
    path = tmp_path / "empty.md"
    path.write_text("   \n\t\n", encoding="utf-8")
    result = extractor.extract(path)
    assert result.success is False
    assert result.document_structure is None
    assert result.error.startswith(ERR_EMPTY)
    assert "empty" in result.to_dict()["error"]


def test_missing_file_fails(extractor, tmp_path):
    result = extractor.extract(tmp_path / "ghost.md")
    assert result.success is False
    assert result.error.startswith(ERR_NOT_FOUND)


def test_directory_and_unsupported_fails(extractor, tmp_path):
    result = extractor.extract(tmp_path)                       # 目录
    assert result.success is False and result.error.startswith(ERR_READ)

    pdf = tmp_path / "book.pdf"
    pdf.write_text("fake", encoding="utf-8")
    result = extractor.extract(pdf)                            # 不支持的后缀
    assert result.success is False
    assert result.error.startswith(ERR_NOT_SUPPORTED)
    assert "docling" not in result.extractor_name              # 未接入外部引擎


def test_invalid_encoding_fails(extractor, tmp_path):
    path = tmp_path / "gbk.md"
    path.write_bytes(b"chapter\xff\xfe\xfa\x01binary garbage")
    result = extractor.extract(path)
    assert result.success is False
    assert result.error.startswith(ERR_DECODE)


def test_txt_no_headings_synthesizes_section(extractor, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("第一段。\n\n第二段，含公式 $a^2+b^2=c^2$。\n", encoding="utf-8")
    result = extractor.extract(path)
    assert result.success is True
    doc = result.document_structure
    assert doc.title == "notes"
    assert len(doc.sections) == 1
    assert doc.sections[0].level == 1 and doc.sections[0].title == "全文"
    assert len(doc.paragraphs) == 2
    assert doc.formulas[0].latex == "a^2+b^2=c^2"


# ---------------------------------------------------------------------------
# 5. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(md_result):
    doc = md_result.document_structure
    d = doc.to_dict()
    text = json.dumps(d, ensure_ascii=False)          # 全 JSON 原生类型
    parsed = json.loads(text)
    assert parsed["title"] == "电磁场导论"
    assert parsed["sections"][1]["formulas"][0]["display"] is True
    assert parsed["sections"][1]["images"][0]["path"] == "figs/wave.png"

    result_parsed = json.loads(md_result.to_json())
    assert result_parsed["success"] is True
    assert result_parsed["stats"]["sections"] == 4

    # 失败结果同样可序列化（document_structure=None）
    failed = DocumentExtractionResult(success=False, error=f"{ERR_EMPTY}: x")
    parsed_fail = json.loads(failed.to_json())
    assert parsed_fail["success"] is False and parsed_fail["document_structure"] is None


# ---------------------------------------------------------------------------
# 6. 与已有 DocumentStructure validator 兼容
# ---------------------------------------------------------------------------

def test_compatible_with_curriculum_validator(md_result):
    structure = to_document_structure(md_result.document_structure)
    assert structure.validate() == []                 # 零结构问题
    # 章脊柱与层级关系保持
    assert [c.title for c in structure.chapter_sections] == ["电磁场导论", "天线基础"]
    wave = next(s for s in structure.sections if s.title == "均匀平面波")
    assert wave.parent_id == structure.sections[0].id
    assert wave.source_page_range is None             # md 无页概念


def test_normalizer_rejects_empty(extractor, tmp_path):
    path = tmp_path / "empty2.md"
    path.write_text("", encoding="utf-8")
    result = extractor.extract(path)
    with pytest.raises(Exception):
        to_document_structure(result.document_structure)


# ---------------------------------------------------------------------------
# 7. 注册表 + 能力探测
# ---------------------------------------------------------------------------

def test_registry_resolve(extractor, tmp_path):
    registry = ExtractorRegistry()
    registry.register(extractor, priority=90)
    assert registry.resolve(tmp_path / "a.md") is extractor
    assert registry.resolve(tmp_path / "a.txt") is extractor
    assert registry.resolve(tmp_path / "a.pdf") is None       # 全缺 → None 降级

    names = registry.registered_names()
    assert names == ("native",)


def test_can_extract_is_readonly(extractor, tmp_path):
    path = tmp_path / "doc.md"
    path.write_text("x", encoding="utf-8")
    assert extractor.can_extract(path) is True
    # 格式路由不要求文件存在（存在性由 extract 判定并返回失败结果）
    assert extractor.can_extract(tmp_path / "missing.md") is True
    assert extractor.can_extract(tmp_path / "doc.pdf") is False
    assert extractor.can_extract(tmp_path / "doc") is False
