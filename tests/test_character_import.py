"""Character card import acceptance tests.

Covered (task §5): import firefly test card, import raiden test card,
auto-registration into list_characters, switch readiness, restart persistence,
conflict safety (abort default / replace / new_id), zip support, validation.

All imports go to a temp base — never the real character/ directory.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from character.character_card import (
    detect_conflict,
    import_character,
    validate_card,
)
from character.character_loader import (
    CharacterLoader,
    list_characters,
    resolve_animations_dir,
)

_YAML_TPL = "version: 1\nprompt: |-\n  测试角色 {name} 的人格描述。\n"


def _write_yaml(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_YAML_TPL.format(name=name), encoding="utf-8")


def _make_card(base: Path, card_id: str, display_name: str) -> Path:
    root = base / f"{card_id}.character"
    (root / "animations").mkdir(parents=True)
    _write_yaml(root / "character" / "identity.yaml", display_name)
    for name in ("personality.yaml", "dialogue_policy.yaml", "relationship_policy.yaml"):
        _write_yaml(root / "character" / name, display_name)
    (root / "manifest.yaml").write_text(
        "character_id: " + card_id + "\n"
        "display_name: " + display_name + "\n"
        "author: test\n"
        "version: 0.0.1\n"
        "description: 测试角色卡\n",
        encoding="utf-8",
    )
    (root / "animations" / "idle.gif").write_bytes(b"GIF89a-test-frame")
    (root / "preview.png").write_bytes(b"fake-png")
    return root


def _identity_with(card_id: str, display_name: str) -> str:
    return (
        "version: 1\n"
        f"display_name: {display_name}\n"
        f"brand_name: {display_name} AI Pet\n"
        "prompt: |-\n"
        f"  你是{display_name}，测试用角色。\n"
    )


def _make_valid_card(base: Path, card_id: str, display_name: str) -> Path:
    """与 _make_card 相同但 identity.yaml 满足 loader 要求（version 1 + display_name）。"""
    root = base / f"{card_id}.character"
    (root / "animations").mkdir(parents=True)
    (root / "character").mkdir(parents=True)
    (root / "character" / "identity.yaml").write_text(
        _identity_with(card_id, display_name), encoding="utf-8"
    )
    for name in ("personality.yaml", "dialogue_policy.yaml", "relationship_policy.yaml"):
        _write_yaml(root / "character" / name, display_name)
    (root / "manifest.yaml").write_text(
        f"character_id: {card_id}\n"
        f"display_name: {display_name}\n"
        "author: test\n"
        "version: 0.0.1\n",
        encoding="utf-8",
    )
    (root / "animations" / "idle.gif").write_bytes(b"GIF89a-test-frame")
    return root


def _zip_card(card_dir: Path, out_zip: Path) -> Path:
    with zipfile.ZipFile(out_zip, "w") as zf:
        for path in card_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(card_dir.parent))
    return out_zip


@pytest.fixture()
def dest_base(tmp_path):
    return tmp_path / "characters"


# ---------------------------------------------------------------------------
# 导入 → 自动注册
# ---------------------------------------------------------------------------

def test_import_new_character_registers(dest_base):
    card = _make_valid_card(dest_base.parent, "tester", "测试者")
    result = import_character(card, dest_base=dest_base)
    assert result["ok"] is True
    assert result["character_id"] == "tester"

    metas = {m.character_id: m for m in list_characters(dest_base)}
    assert metas["tester"].display_name == "测试者"
    assert metas["tester"].animations_dir == dest_base / "tester" / "animations"


def test_import_firefly_and_raiden_test_cards(dest_base):
    f_card = _make_valid_card(dest_base.parent, "firefly", "流萤")
    r_card = _make_valid_card(dest_base.parent, "raiden", "雷电将军")
    assert import_character(f_card, dest_base=dest_base)["ok"]
    assert import_character(r_card, dest_base=dest_base)["ok"]
    ids = {m.character_id for m in list_characters(dest_base)}
    assert {"firefly", "raiden"} <= ids


# ---------------------------------------------------------------------------
# 冲突安全
# ---------------------------------------------------------------------------

def test_import_conflict_abort_default(dest_base):
    card = _make_valid_card(dest_base.parent, "dup", "角色甲")
    import_character(card, dest_base=dest_base)
    # 同 id 再导入（默认 abort）→ 不覆盖
    marker = dest_base / "dup" / "marker.txt"
    marker.write_text("original", encoding="utf-8")
    result = import_character(card, dest_base=dest_base)
    assert result["ok"] is False
    assert result["action"] == "conflict"
    assert marker.read_text(encoding="utf-8") == "original"  # 未覆盖


def test_import_conflict_replace(dest_base):
    card = _make_valid_card(dest_base.parent, "rep", "角色乙")
    import_character(card, dest_base=dest_base)
    dest = dest_base / "rep"
    (dest / "extra.txt").write_text("keep", encoding="utf-8")
    result = import_character(card, dest_base=dest_base, mode="replace")
    assert result["ok"] is True and result["action"] == "replace"
    assert (dest / "identity.yaml").is_file()          # 被替换内容仍完整
    assert not (dest / "extra.txt").exists()           # 目录已重建（覆盖语义）


def test_import_conflict_new_id(dest_base):
    card = _make_valid_card(dest_base.parent, "dup2", "角色丙")
    import_character(card, dest_base=dest_base)
    result = import_character(card, dest_base=dest_base, mode="new_id")
    assert result["ok"] is True and result["action"] == "new_id"
    assert result["character_id"].startswith("dup2-imported")
    ids = {m.character_id for m in list_characters(dest_base)}
    assert "dup2" in ids and result["character_id"] in ids


def test_detect_conflict(dest_base):
    assert detect_conflict("missing", dest_base) is False
    _make_valid_card(dest_base.parent, "exist", "存在")
    import_character(dest_base.parent / "exist.character", dest_base=dest_base)
    assert detect_conflict("exist", dest_base) is True


# ---------------------------------------------------------------------------
# zip 与校验
# ---------------------------------------------------------------------------

def test_import_zip_card(dest_base):
    card = _make_valid_card(dest_base.parent, "zipped", "压缩角色")
    zipped = _zip_card(card, dest_base.parent / "zipped.character.zip")
    validation = validate_card(zipped)
    assert validation.ok is True
    result = import_character(zipped, dest_base=dest_base)
    assert result["ok"] is True and result["character_id"] == "zipped"


def test_import_invalid_rejected(dest_base):
    bad = dest_base.parent / "bad.character"
    (bad / "character").mkdir(parents=True)
    (bad / "manifest.yaml").write_text("character_id: bad\n", encoding="utf-8")
    result = import_character(bad, dest_base=dest_base)
    assert result["ok"] is False
    assert result["action"] == "invalid"


def test_import_bad_character_id_rejected(dest_base):
    bad = dest_base.parent / "Bad..character"
    (bad / "character").mkdir(parents=True)
    (bad / "manifest.yaml").write_text(
        "character_id: '../evil'\ndisplay_name: 恶意\n", encoding="utf-8"
    )
    validation = validate_card(bad)
    assert validation.ok is False
    assert any("character_id" in e for e in validation.errors)


# ---------------------------------------------------------------------------
# 重启持久化 + 可切换
# ---------------------------------------------------------------------------

def test_import_persists_across_restart(dest_base):
    card = _make_valid_card(dest_base.parent, "persist", "持久角色")
    import_character(card, dest_base=dest_base)

    # 模拟重启：重新扫描 + 重新加载（同一文件系统）
    metas = {m.character_id: m for m in list_characters(dest_base)}
    assert "persist" in metas

    profile = CharacterLoader(
        dest_base / "persist", character_id="persist"
    ).load()
    assert profile.display_name == "持久角色"
    assert profile.identity_prompt  # 人格已加载

    metas = {m.character_id: m for m in list_characters(dest_base)}
    assert metas["persist"].animations_dir == dest_base / "persist" / "animations"


def test_resolve_animations_dir_repo_layout(tmp_path):
    """resolve_animations_dir 仓库布局：character/<id>/animations 被识别。"""
    repo = tmp_path
    (repo / "character" / "custom_01" / "animations").mkdir(parents=True)
    resolved = resolve_animations_dir("custom_01", repo)
    assert resolved == repo / "character" / "custom_01" / "animations"
