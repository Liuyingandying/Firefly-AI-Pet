# -*- coding: utf-8 -*-
"""角色管理：描述 / 详情 / 导出（复用 list_characters，不触碰加载与切换核心）。

生命周期闭环：导入（character_card）→ 查看（describe/detail）→ 使用（既有切换）
→ 导出（export_character → 可重新导入的 xxx.character.zip）。

导出产物保证可通过 ``character_card.validate_card`` 重新导入：
- manifest.yaml（内置角色无 manifest 时按 identity.yaml 现场生成）
- character/identity|personality|dialogue_policy|relationship_policy.yaml
- animations/*.gif（有则带）
- preview.png（有则带）
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import yaml

from character.character_loader import (
    CHARACTER_BASE_DIR,
    list_characters,
    load_current_character,
)

_PERSONA_FILES = (
    "identity.yaml",
    "personality.yaml",
    "dialogue_policy.yaml",
    "relationship_policy.yaml",
)


def _read_manifest(char_dir: Path) -> dict[str, Any]:
    path = char_dir / "manifest.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return document if isinstance(document, dict) else {}
    except Exception:  # noqa: BLE001 - 缺/坏 manifest 按空处理
        return {}


def _identity_display_name(char_dir: Path, fallback: str) -> str:
    try:
        document = yaml.safe_load(
            (char_dir / "identity.yaml").read_text(encoding="utf-8")
        ) or {}
        raw = document.get("display_name") if isinstance(document, dict) else None
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    except Exception:  # noqa: BLE001
        pass
    return fallback


def describe_characters(base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """列出全部角色（来源 = list_characters，含管理元数据）。"""
    base = Path(base_dir or CHARACTER_BASE_DIR)
    out: list[dict[str, Any]] = []
    for meta in list_characters(base):
        char_dir = base / meta.character_id
        manifest = _read_manifest(char_dir)
        preview = char_dir / "preview.png"
        out.append(
            {
                "character_id": meta.character_id,
                "display_name": str(manifest.get("display_name") or meta.display_name),
                "author": str(manifest.get("author") or "—"),
                "version": str(manifest.get("version") or "—"),
                "description": str(manifest.get("description") or ""),
                "has_manifest": (char_dir / "manifest.yaml").is_file(),
                "has_preview": preview.is_file(),
                "preview_path": str(preview) if preview.is_file() else None,
            }
        )
    return out


def character_detail(
    character_id: str, base_dir: str | Path | None = None
) -> dict[str, Any]:
    """单个角色的详情：头像 / 动画状态 / 人格状态 / 是否当前启用。"""
    base = Path(base_dir or CHARACTER_BASE_DIR)
    char_dir = base / character_id
    if not char_dir.is_dir():
        return {"ok": False, "error": "unknown_character", "character_id": character_id}

    animations_dir = char_dir / "animations"
    gifs = sorted(animations_dir.glob("*.gif")) if animations_dir.is_dir() else []
    persona = {
        name: (char_dir / name).is_file()
        for name in _PERSONA_FILES
    }
    manifest = _read_manifest(char_dir)
    return {
        "ok": True,
        "character_id": character_id,
        "display_name": str(manifest.get("display_name") or character_id),
        "author": str(manifest.get("author") or "—"),
        "version": str(manifest.get("version") or "—"),
        "description": str(manifest.get("description") or ""),
        "preview_path": str(char_dir / "preview.png")
        if (char_dir / "preview.png").is_file()
        else None,
        "animations": {
            "present": len(gifs) > 0,
            "gif_count": len(gifs),
            "gifs": [g.name for g in gifs],
        },
        "persona": persona,
        "persona_complete": all(persona.values()),
        "is_current": load_current_character() == character_id,
    }


def export_character(
    character_id: str,
    out_zip: str | Path,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """导出 character/<id>/ 为可重新导入的 xxx.character.zip。"""
    base = Path(base_dir or CHARACTER_BASE_DIR)
    char_dir = base / character_id
    out = Path(out_zip)
    if not char_dir.is_dir():
        return {"ok": False, "error": "unknown_character", "character_id": character_id}

    manifest = _read_manifest(char_dir)
    if not manifest:
        manifest = {
            "character_id": character_id,
            "display_name": _identity_display_name(char_dir, character_id),
            "author": "firefly",
            "version": "1.0.0",
        }

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in _PERSONA_FILES:
                src = char_dir / name
                if src.is_file():
                    zf.write(src, f"character/{name}")
            animations_dir = char_dir / "animations"
            if animations_dir.is_dir():
                for gif in sorted(animations_dir.glob("*.gif")):
                    zf.write(gif, f"animations/{gif.name}")
            preview = char_dir / "preview.png"
            if preview.is_file():
                zf.write(preview, "preview.png")
            zf.writestr(
                "manifest.yaml",
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
            )
    except OSError as exc:
        return {"ok": False, "error": f"export_failed:{type(exc).__name__}"}

    return {
        "ok": True,
        "character_id": character_id,
        "display_name": str(manifest.get("display_name") or character_id),
        "zip_path": str(out),
        "message": f"已导出 {character_id} → {out.name}",
    }


def open_character_manager(parent=None, base_dir: str | Path | None = None) -> Any:
    """GUI 管理窗口（无 Qt 环境返回 None）。"""
    try:
        from ui.character_manager import CharacterManagerDialog
    except Exception:  # noqa: BLE001
        return None
    dialog = CharacterManagerDialog(base_dir=base_dir, parent=parent)
    dialog.exec()
    return dialog
