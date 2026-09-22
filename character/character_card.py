# -*- coding: utf-8 -*-
"""角色卡导入系统（Character Card Import）。

复用现有角色体系，**不建立第二套管理**：
- 导入目标 = ``character/<character_id>/``（list_characters() 自动发现）
- 动画目录 = ``character/<character_id>/animations/``（resolve_animations_dir 已识别）
- 切换 = 现有“切换角色”菜单（零改动）

卡片规范（``xxx.character`` 文件夹或 ``xxx.character.zip``）::

    manifest.yaml            character_id / display_name / author / version /
                             description / required_files（可选）
    character/              identity.yaml 必选；personality / dialogue_policy /
                            relationship_policy 有则复制
    animations/             *.gif（可选，但提供则校验非空 GIF）
    preview.png             预览图（可选）

导入策略（默认安全）：
- ``abort``（默认）：character_id 已存在 → 不覆盖，返回冲突
- ``replace``：覆盖既有角色目录
- ``new_id``：另存为 ``<id>-imported-N``
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from character.character_loader import CHARACTER_BASE_DIR, list_characters

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REQUIRED_CHARACTER_FILES = (
    "identity.yaml",
    "personality.yaml",
    "dialogue_policy.yaml",
    "relationship_policy.yaml",
)


@dataclass(frozen=True)
class CharacterCardManifest:
    character_id: str
    display_name: str
    author: str = ""
    version: str = "0.0.0"
    description: str = ""
    required_files: tuple[str, ...] = field(
        default_factory=lambda: tuple(REQUIRED_CHARACTER_FILES)
    )


@dataclass
class CardValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    manifest: CharacterCardManifest | None = None
    source_root: Path | None = None


# ---------------------------------------------------------------------------
# 定位卡片根
# ---------------------------------------------------------------------------

def _locate_card_root(source: Path) -> Path | None:
    """返回含 manifest.yaml 的根目录（支持 zip 解包到临时目录时再定位）。"""
    if source.is_file() and source.suffix.lower() == ".zip":
        return None  # zip 需先解包
    root = source
    if (root / "manifest.yaml").is_file():
        return root
    # 兼容：source 指向 xxx.character/character/ 之类子目录
    for parent in (root.parent,):
        if (parent / "manifest.yaml").is_file():
            return parent
    return None


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _cleanup(dir_path: Path | None) -> None:
    if dir_path is not None:
        try:
            shutil.rmtree(dir_path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def validate_card(source: Path, *, temporary_dir: Path | None = None) -> CardValidation:
    """校验一张角色卡（文件夹或 zip）。zip 解包到 ``temporary_dir`` 后校验。"""
    errors: list[str] = []

    if source.is_file() and source.suffix.lower() == ".zip":
        extract_to = Path(tempfile.mkdtemp(prefix="firefly_card_"))
        try:
            with zipfile.ZipFile(source) as zf:
                zf.extractall(extract_to)
        except (zipfile.BadZipFile, OSError) as exc:
            _cleanup(extract_to)
            return CardValidation(ok=False, errors=[f"zip 解包失败: {exc}"])
        root = _find_manifest_root(extract_to)
        if root is None:
            _cleanup(extract_to)
            return CardValidation(ok=False, errors=["zip 内未找到 manifest.yaml"])
        result = _validate_root(root, errors)
        result.source_root = root
        if not result.ok:
            _cleanup(extract_to)
        return result

    root = _locate_card_root(source)
    if root is None:
        return CardValidation(ok=False, errors=["未找到 manifest.yaml"])
    return _validate_root(root, errors)


def _find_manifest_root(base: Path) -> Path | None:
    if (base / "manifest.yaml").is_file():
        return base
    children = [c for c in base.iterdir() if c.is_dir()]
    if len(children) == 1 and (children[0] / "manifest.yaml").is_file():
        return children[0]
    return None


def _validate_root(root: Path, errors: list[str]) -> CardValidation:
    try:
        manifest_data = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"manifest.yaml 读取失败: {exc}")
        return CardValidation(ok=False, errors=errors)

    if not isinstance(manifest_data, dict):
        errors.append("manifest.yaml 顶层必须是映射")
        return CardValidation(ok=False, errors=errors)

    character_id = str(manifest_data.get("character_id") or "").strip()
    display_name = str(manifest_data.get("display_name") or "").strip()
    if not _ID_RE.match(character_id):
        errors.append(
            f"character_id 非法: {character_id!r}（须小写字母/数字/下划线/连字符，64 字符内）"
        )
    if not display_name:
        errors.append("display_name 缺失")

    character_dir = root / "character"
    if not (character_dir / "identity.yaml").is_file():
        errors.append("character/identity.yaml 缺失（必须）")

    required = manifest_data.get("required_files")
    required_files = tuple(REQUIRED_CHARACTER_FILES)
    if isinstance(required, list):
        required_files = tuple(
            str(item) for item in required if isinstance(item, str)
        )
    for name in required_files:
        if not (character_dir / name).is_file():
            errors.append(f"required_files 缺失: {name}")

    animations_dir = root / "animations"
    if animations_dir.is_dir():
        gifs = sorted(animations_dir.glob("*.gif"))
        if not gifs:
            errors.append("animations/ 存在但无 .gif 文件")
        for gif in gifs:
            if gif.stat().st_size == 0:
                errors.append(f"animations/{gif.name} 为空文件")

    if errors:
        return CardValidation(ok=False, errors=errors)

    return CardValidation(
        ok=True,
        manifest=CharacterCardManifest(
            character_id=character_id,
            display_name=display_name,
            author=str(manifest_data.get("author") or ""),
            version=str(manifest_data.get("version") or "0.0.0"),
            description=str(manifest_data.get("description") or ""),
            required_files=required_files,
        ),
        source_root=root,
    )


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def detect_conflict(character_id: str, dest_base: Path | None = None) -> bool:
    base = Path(dest_base or CHARACTER_BASE_DIR)
    return (base / character_id).is_dir()


def _unique_id(character_id: str, base: Path) -> str:
    candidate, n = character_id, 1
    while (base / candidate).is_dir():
        n += 1
        candidate = f"{character_id}-imported-{n}"
    return candidate


def import_character(
    source: Path,
    *,
    dest_base: Path | None = None,
    mode: str = "abort",
) -> dict[str, Any]:
    """导入一张角色卡到 character/<id>/。

    ``mode``: abort(默认, 冲突不覆盖) | replace | new_id。
    返回 {ok, character_id, action, message, display_name}。
    """
    validation = validate_card(source)
    if not validation.ok or validation.manifest is None:
        return {"ok": False, "action": "invalid", "errors": validation.errors}
    manifest = validation.manifest
    base = Path(dest_base or CHARACTER_BASE_DIR)
    card_root = validation.source_root
    assert card_root is not None

    if mode not in ("abort", "replace", "new_id"):
        mode = "abort"

    target_id = manifest.character_id
    if detect_conflict(target_id, base):
        if mode == "abort":
            return {
                "ok": False,
                "action": "conflict",
                "character_id": target_id,
                "display_name": manifest.display_name,
                "message": "已存在同名角色（默认不覆盖）",
            }
        if mode == "new_id":
            target_id = _unique_id(target_id, base)

    dest = base / target_id
    try:
        if mode == "replace" and dest.is_dir():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        character_src = card_root / "character"
        for yaml_file in character_src.glob("*.yaml"):
            shutil.copy2(yaml_file, dest / yaml_file.name)

        animations_src = card_root / "animations"
        if animations_src.is_dir():
            dest_anims = dest / "animations"
            dest_anims.mkdir(exist_ok=True)
            for gif in animations_src.glob("*.gif"):
                shutil.copy2(gif, dest_anims / gif.name)

        preview_src = card_root / "preview.png"
        if preview_src.is_file():
            shutil.copy2(preview_src, dest / "preview.png")

        manifest_copy = dest / "manifest.yaml"
        manifest_copy.write_text(
            yaml.safe_dump(
                {
                    "character_id": target_id,
                    "display_name": manifest.display_name,
                    "author": manifest.author,
                    "version": manifest.version,
                    "description": manifest.description,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return {"ok": False, "action": "copy_error", "errors": [str(exc)]}

    return {
        "ok": True,
        "action": mode,
        "character_id": target_id,
        "display_name": manifest.display_name,
        "message": f"已导入 {manifest.display_name}（{target_id}）",
    }


# ---------------------------------------------------------------------------
# GUI 包装（托盘/设置入口复用；无 GUI 环境自动跳过）
# ---------------------------------------------------------------------------

def pick_and_import_character_card(parent=None, dest_base: Path | None = None) -> dict[str, Any]:
    """弹窗选择 .character 文件夹或 zip，按冲突策略导入。

    仅用于有 GUI 的环境；无界面调用方应直接用 validate_card / import_character。
    """
    try:
        from PySide6.QtWidgets import QFileDialog, QMessageBox
    except Exception:  # noqa: BLE001 - 无 Qt 环境返回不可用
        return {"ok": False, "action": "no_gui"}

    choice = QMessageBox(
        QMessageBox.Icon.Question,
        "导入角色卡",
        "选择导入来源：",
        QMessageBox.StandardButton.NoButton,
        parent,
    )
    folder_btn = choice.addButton("选择 .character 文件夹", QMessageBox.ButtonRole.AcceptRole)
    zip_btn = choice.addButton("选择 zip 包", QMessageBox.ButtonRole.AcceptRole)
    choice.addButton(QMessageBox.StandardButton.Cancel)
    choice.exec()
    clicked = choice.clickedButton()
    if clicked is folder_btn:
        selected = QFileDialog.getExistingDirectory(parent, "选择角色卡文件夹")
    elif clicked is zip_btn:
        path, _ = QFileDialog.getOpenFileName(
            parent, "选择角色卡 zip", "", "角色卡 zip (*.zip)"
        )
        selected = path
    else:
        return {"ok": False, "action": "cancelled"}

    if not selected:
        return {"ok": False, "action": "cancelled"}

    validation = validate_card(Path(selected))
    if not validation.ok or validation.manifest is None:
        QMessageBox.warning(parent, "导入角色卡", "校验失败：\n" + "\n".join(validation.errors))
        return {"ok": False, "action": "invalid", "errors": validation.errors}

    manifest = validation.manifest
    mode = "abort"
    if detect_conflict(manifest.character_id, dest_base):
        confirm = QMessageBox(
            QMessageBox.Icon.Warning,
            "角色已存在",
            f"character_id「{manifest.character_id}」已存在，如何处理？",
            QMessageBox.StandardButton.NoButton,
            parent,
        )
        replace_btn = confirm.addButton("覆盖", QMessageBox.ButtonRole.AcceptRole)
        new_id_btn = confirm.addButton("另存新 ID", QMessageBox.ButtonRole.AcceptRole)
        confirm.addButton(QMessageBox.StandardButton.Cancel)
        confirm.exec()
        picked = confirm.clickedButton()
        if picked is replace_btn:
            mode = "replace"
        elif picked is new_id_btn:
            mode = "new_id"
        else:
            return {"ok": False, "action": "cancelled"}

    result = import_character(Path(selected), dest_base=dest_base, mode=mode)
    if result.get("ok"):
        QMessageBox.information(parent, "导入角色卡", result.get("message", "导入成功"))
    return result
