"""Firefly Skin System M1 — install / uninstall (abort/replace/new_id)."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from skins.registry import clear_active_skin, list_skins
from skins.validator import _safe_member, validate_skin


def _unique_id(skin_id: str, base: Path) -> str:
    candidate, n = skin_id, 1
    while (base / candidate).is_dir():
        n += 1
        candidate = f"{skin_id}-imported-{n}"
    return candidate


def _copy_card(root: Path, dest: Path, skin_id: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("metadata.json", "theme.json", "preview.png", "README.md"):
        src = root / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    animations = root / "animations"
    if animations.is_dir():
        dest_anim = dest / "animations"
        dest_anim.mkdir(exist_ok=True)
        for gif in animations.glob("*.gif"):
            shutil.copy2(gif, dest_anim / gif.name)


def install_skin(source: Path, *, root: Path | None = None, mode: str = "abort") -> dict[str, Any]:
    """安装皮肤到 skins/<skin_id>/。mode: abort | replace | new_id。"""
    base = Path(root or _default_root())
    validation = validate_skin(source)
    if not validation.ok or validation.manifest is None:
        return {"ok": False, "action": "invalid", "errors": validation.errors}
    manifest = validation.manifest

    if mode not in ("abort", "replace", "new_id"):
        mode = "abort"

    target_id = manifest.skin_id
    if (base / target_id).is_dir():
        if mode == "abort":
            return {
                "ok": False, "action": "conflict", "skin_id": target_id,
                "display_name": manifest.display_name,
                "message": "已存在同名皮肤（默认不覆盖）",
            }
        if mode == "new_id":
            target_id = _unique_id(target_id, base)

    try:
        dest = base / target_id
        if mode == "replace" and dest.is_dir():
            shutil.rmtree(dest)
        if validation.source_root is not None:
            _copy_card(validation.source_root, dest, target_id)
        else:
            # zip：安全解包后复制
            extract_to = Path(tempfile.mkdtemp(prefix="firefly_skin_"))
            try:
                with zipfile.ZipFile(source) as zf:
                    from tools.character_package.validator import extract_validated_archive
                    extract_validated_archive(zf, extract_to)
                root_dir = extract_to
                if (extract_to / "metadata.json").is_file():
                    pass
                else:
                    children = [c for c in extract_to.iterdir() if c.is_dir()]
                    if len(children) == 1:
                        root_dir = children[0]
                _copy_card(root_dir, dest, target_id)
            finally:
                shutil.rmtree(extract_to, ignore_errors=True)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "action": "copy_error", "errors": [str(exc)]}

    return {
        "ok": True, "action": mode, "skin_id": target_id,
        "display_name": manifest.display_name,
        "message": f"已安装 {manifest.display_name}（{target_id}）",
    }


def uninstall_skin(skin_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """卸载皮肤：删除目录并清空引用它的角色激活项。"""
    base = Path(root or _default_root())
    dest = base / skin_id
    if not dest.is_dir():
        return {"ok": False, "action": "missing", "skin_id": skin_id}
    try:
        shutil.rmtree(dest)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {"ok": False, "action": "remove_error", "errors": [str(exc)]}
    # 清理引用
    try:
        from skins.registry import active_skins_file

        import yaml

        path = active_skins_file(root)
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                for char, sid in list(data.items()):
                    if sid == skin_id:
                        clear_active_skin(str(char), root)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "action": "uninstalled", "skin_id": skin_id}


def _default_root():
    from skins.registry import default_skins_root

    return default_skins_root()
