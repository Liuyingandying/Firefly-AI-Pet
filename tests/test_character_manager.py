"""Character management lifecycle tests: import → view → export → re-import → switch.

All operations use a temp base directory — the real character/ is never touched.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from character.character_card import import_character, validate_card
from character.character_loader import CharacterLoader, list_characters, resolve_animations_dir
from character.character_manager import (
    character_detail,
    describe_characters,
    export_character,
)


def _make_card(base: Path, card_id: str, display_name: str) -> Path:
    root = base / f"{card_id}.character"
    (root / "animations").mkdir(parents=True)
    (root / "character").mkdir(parents=True)
    (root / "character" / "identity.yaml").write_text(
        "version: 1\n"
        f"display_name: {display_name}\n"
        "prompt: |-\n"
        f"  你是{display_name}。\n",
        encoding="utf-8",
    )
    for name in ("personality.yaml", "dialogue_policy.yaml", "relationship_policy.yaml"):
        (root / "character" / name).write_text(
            "version: 1\nprompt: |-\n  测试。\n", encoding="utf-8"
        )
    (root / "manifest.yaml").write_text(
        f"character_id: {card_id}\n"
        f"display_name: {display_name}\n"
        "author: manager-test\n"
        "version: 2.3.4\n"
        "description: 生命周期测试角色\n",
        encoding="utf-8",
    )
    (root / "animations" / "idle.gif").write_bytes(b"GIF89a-frame")
    (root / "preview.png").write_bytes(b"fake-png")
    return root


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture()
def dest_base(tmp_path):
    return tmp_path / "characters"


# ---------------------------------------------------------------------------
# 导入 → 查看
# ---------------------------------------------------------------------------

def test_import_then_describe_lists_meta(dest_base):
    card = _make_card(dest_base.parent, "lifecycle", "生命周期角色")
    assert import_character(card, dest_base=dest_base)["ok"]

    metas = {m["character_id"]: m for m in describe_characters(dest_base)}
    assert "lifecycle" in metas
    meta = metas["lifecycle"]
    assert meta["display_name"] == "生命周期角色"
    assert meta["author"] == "manager-test"
    assert meta["version"] == "2.3.4"
    assert meta["description"] == "生命周期测试角色"
    assert meta["has_preview"] is True and meta["preview_path"]


def test_detail_shows_animations_persona_and_current(dest_base):
    card = _make_card(dest_base.parent, "detail", "详情角色")
    import_character(card, dest_base=dest_base)

    detail = character_detail("detail", dest_base)
    assert detail["ok"] is True
    assert detail["animations"]["present"] is True
    assert detail["animations"]["gif_count"] == 1
    assert detail["animations"]["gifs"] == ["idle.gif"]
    assert detail["persona_complete"] is True
    assert all(detail["persona"].values())
    assert detail["is_current"] in (True, False)  # 取决于持久化选择, 不抛错


def test_detail_missing_character(dest_base):
    detail = character_detail("ghost", dest_base)
    assert detail["ok"] is False


# ---------------------------------------------------------------------------
# 导出 → 重新导入 → 切换
# ---------------------------------------------------------------------------

def test_export_zip_preserves_structure(dest_base, tmp_path):
    card = _make_card(dest_base.parent, "export", "导出角色")
    import_character(card, dest_base=dest_base)

    out_zip = tmp_path / "export.character.zip"
    result = export_character("export", out_zip, dest_base)
    assert result["ok"] is True

    names = zipfile.ZipFile(out_zip).namelist()
    assert "manifest.yaml" in names
    for y in ("identity.yaml", "personality.yaml", "dialogue_policy.yaml", "relationship_policy.yaml"):
        assert f"character/{y}" in names
    assert "animations/idle.gif" in names
    assert "preview.png" in names


def test_export_roundtrip_reimport_and_switch(dest_base, tmp_path):
    """导入 → 导出 → 清空 → 重新导入 → 可切换。"""
    card = _make_card(dest_base.parent, "roundtrip", "往返角色")
    import_character(card, dest_base=dest_base)
    out_zip = tmp_path / "roundtrip.character.zip"
    export_character("roundtrip", out_zip, dest_base)

    # 删除原角色目录 → 模拟全新环境
    import shutil

    shutil.rmtree(dest_base / "roundtrip")
    assert "roundtrip" not in {m.character_id for m in list_characters(dest_base)}

    # 重新导入导出的 zip
    validation = validate_card(out_zip)
    assert validation.ok is True
    assert import_character(out_zip, dest_base=dest_base)["ok"]

    # 可切换：loader 人格 + 动画解析（注册表 animations_dir）
    profile = CharacterLoader(dest_base / "roundtrip", character_id="roundtrip").load()
    assert profile.display_name == "往返角色"
    metas = {m.character_id: m for m in list_characters(dest_base)}
    assert metas["roundtrip"].animations_dir == dest_base / "roundtrip" / "animations"


def test_export_builtin_character_generates_manifest(dest_base):
    """内置角色无 manifest.yaml 时，导出须现场生成合法 manifest。"""
    import shutil

    shutil.copytree(
        Path(__file__).resolve().parents[1] / "character" / "firefly",
        dest_base / "firefly",
    )
    out_zip = dest_base.parent / "firefly_export.character.zip"
    result = export_character("firefly", out_zip, dest_base)
    assert result["ok"] is True

    names = zipfile.ZipFile(out_zip).namelist()
    assert "manifest.yaml" in names
    assert "character/identity.yaml" in names
    validation = validate_card(out_zip)
    assert validation.ok is True


def test_manager_dialog_constructs_offscreen(qapp, dest_base):
    """对话框离屏构建冒烟：列表含导入角色（Qt 可实例化）。"""
    card = _make_card(dest_base.parent, "uicheck", "界面对话")
    import_character(card, dest_base=dest_base)

    from PySide6.QtWidgets import QApplication

    from ui.character_manager import CharacterManagerDialog

    _app = QApplication.instance() or QApplication([])
    dialog = CharacterManagerDialog(base_dir=dest_base)
    count = dialog._list.count()
    dialog.close()
    assert count >= 1
    assert any("uicheck" in dialog._list.item(i).text() for i in range(count))
