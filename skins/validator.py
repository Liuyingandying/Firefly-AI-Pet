"""Firefly Skin System M1 — zip validator (pure data, zero execution)."""

from __future__ import annotations

import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skins.schema import SkinManifest, parse_manifest

_BLOCKED_SUFFIXES = (".py", ".pyd", ".exe", ".so", ".dll", ".bat", ".cmd", ".ps1")
_BLOCKED_PARTS = ("__pycache__",)


@dataclass
class SkinValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    manifest: SkinManifest | None = None
    source_root: Path | None = None


def _safe_member(member: str) -> str | None:
    """Normalize a zip member name; None when unsafe (traversal/absolute)."""
    name = member.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        return None
    if any(p == ".." for p in parts):
        return None
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    return "/".join(parts)


def _find_root_dir(base: Path) -> Path | None:
    if (base / "metadata.json").is_file():
        return base
    children = [c for c in base.iterdir() if c.is_dir()]
    if len(children) == 1 and (children[0] / "metadata.json").is_file():
        return children[0]
    return None


def validate_skin(source: Path, *, temporary_dir: Path | None = None) -> SkinValidation:
    """Validate a skin: folder or .skin.zip. Pure data; never executes code."""
    errors: list[str] = []

    if source.is_file() and source.suffix.lower() == ".zip":
        extract_to = Path(tempfile.mkdtemp(prefix="firefly_skin_"))
        try:
            with zipfile.ZipFile(source) as zf:
                for info in zf.infolist():
                    safe = _safe_member(info.filename)
                    if safe is None:
                        errors.append(f"zip 成员非法（路径穿越/绝对路径）: {info.filename}")
                        continue
                    if info.filename.lower().endswith(_BLOCKED_SUFFIXES):
                        errors.append(f"zip 含可执行文件（禁止）: {info.filename}")
                    if any(part in _BLOCKED_PARTS for part in safe.split("/")):
                        errors.append(f"zip 含缓存/系统目录: {info.filename}")
                if not errors:
                    from tools.character_package.validator import extract_validated_archive
                    extract_validated_archive(zf, extract_to)
        except (zipfile.BadZipFile, OSError, ValueError, RuntimeError) as exc:
            return SkinValidation(ok=False, errors=[f"zip 损坏: {exc}"])
        if errors:
            return SkinValidation(ok=False, errors=errors, source_root=None)
        root = _find_root_dir(extract_to)
        if root is None:
            return SkinValidation(ok=False, errors=["zip 内未找到 metadata.json"])
        result = _validate_root(root, errors)
        result.source_root = root
        if not result.ok:
            return result
        return result

    root = _find_root_dir(source)
    if root is None:
        return SkinValidation(ok=False, errors=["未找到 metadata.json"])
    return _validate_root(root, errors)


def _validate_root(root: Path, errors: list[str]) -> SkinValidation:
    meta_path = root / "metadata.json"
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return SkinValidation(ok=False, errors=[f"metadata.json 读取失败: {exc}"])

    manifest: SkinManifest | None = None
    try:
        manifest = parse_manifest(raw)
    except Exception as exc:  # noqa: BLE001 - SchemaError 汇总
        return SkinValidation(ok=False, errors=[f"metadata 校验失败: {exc}"])

    for suffix in _BLOCKED_SUFFIXES:
        for blocked in root.rglob(f"*{suffix}"):
            errors.append(f"含可执行文件（禁止）: {blocked.name}")
    if any(p.name in _BLOCKED_PARTS for p in root.rglob("*")):
        errors.append("含 __pycache__/系统目录")

    animations = root / "animations"
    if animations.is_dir():
        gifs = sorted(animations.glob("*.gif"))
        if not gifs:
            errors.append("animations/ 存在但无 .gif")
        elif not (animations / f"{manifest.fallback_state}.gif").is_file():
            errors.append(f"缺少 fallback 动画 {manifest.fallback_state}.gif")
        for gif in gifs:
            if gif.stat().st_size == 0:
                errors.append(f"animations/{gif.name} 为空")
    else:
        errors.append("animations/ 缺失")

    if errors:
        return SkinValidation(ok=False, errors=errors)
    return SkinValidation(ok=True, manifest=manifest, source_root=root)
