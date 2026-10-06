"""Firefly Skin System M1 — registry + active-skin persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from skins.schema import SkinManifest, parse_manifest

SKIN_ID_RE = None  # 复用 schema.SKIN_ID_RE


@dataclass(frozen=True)
class SkinMeta:
    skin_id: str
    display_name: str
    author: str
    version: str
    description: str
    preview_path: str | None = None
    animations_dir: str | None = None


def default_skins_root() -> Path:
    """用户数据根 skins/（与 plugins/ 同级；尊重 FIREFLY_USER_DATA_DIR）。"""
    try:
        from core.user_paths import get_user_data_paths

        return get_user_data_paths().root / "skins"
    except Exception:  # noqa: BLE001
        from pathlib import Path as _P

        return _P(__file__).resolve().parents[1] / "config" / "skins"


def active_skins_file(root: Path | None = None) -> Path:
    return (root or default_skins_root()) / "active.yaml"


def list_skins(root: Path | None = None) -> list[SkinMeta]:
    """扫描 skins/<id>/metadata.json（零硬编码）。"""
    base = Path(root or default_skins_root())
    metas: list[SkinMeta] = []
    if not base.is_dir():
        return metas
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        meta_path = child / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            manifest = parse_manifest(json.loads(meta_path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - 坏卡跳过
            continue
        preview = child / "preview.png"
        animations = child / "animations"
        metas.append(
            SkinMeta(
                skin_id=manifest.skin_id,
                display_name=manifest.display_name,
                author=manifest.author,
                version=manifest.version,
                description=manifest.description,
                preview_path=str(preview) if preview.is_file() else None,
                animations_dir=str(animations) if animations.is_dir() else None,
            )
        )
    return metas


def get_skin(skin_id: str, root: Path | None = None) -> SkinMeta | None:
    for meta in list_skins(root):
        if meta.skin_id == skin_id:
            return meta
    return None


def load_active_skin(character_id: str, root: Path | None = None) -> str | None:
    """character_id -> 激活 skin_id；None = 未指定（回退默认）。"""
    path = active_skins_file(root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        value = data.get(character_id) if isinstance(data, dict) else None
        return str(value).strip() if isinstance(value, str) and value.strip() else None
    except Exception:  # noqa: BLE001
        return None


def save_active_skin(character_id: str, skin_id: str | None, root: Path | None = None) -> bool:
    path = active_skins_file(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            data = loaded if isinstance(loaded, dict) else {}
        if skin_id:
            data[character_id] = skin_id
        else:
            data.pop(character_id, None)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:  # noqa: BLE001
        return False


def clear_active_skin(character_id: str, root: Path | None = None) -> bool:
    return save_active_skin(character_id, None, root)