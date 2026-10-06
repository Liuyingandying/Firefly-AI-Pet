"""Firefly Skin System M1 — animation resolver（皮肤 → 角色 → 默认 三层回退）。"""

from __future__ import annotations

from pathlib import Path

from skins.registry import default_skins_root, load_active_skin


def animation_resolver(
    character_id: str,
    active_skin_id: str | None = None,
    *,
    skins_root: Path | None = None,
    repo_root: Path | None = None,
) -> Path | None:
    """解析角色当前动画目录。

    优先级：激活皮肤 animations → 角色目录（assets/skins/<id> 或 character/<id>）
    → 默认 assets/animations。None = 调用方回退 idle.gif（播放器既有行为）。
    """
    if active_skin_id is None:
        active_skin_id = load_active_skin(character_id, skins_root)
    if active_skin_id:
        skin_anim = Path(skins_root or default_skins_root()) / active_skin_id / "animations"
        if skin_anim.is_dir():
            return skin_anim

    # 角色层（复用既有解析，不触碰其核心）
    try:
        from character.character_loader import resolve_animations_dir

        base = Path(repo_root or Path(__file__).resolve().parents[1])
        character_dir = resolve_animations_dir(character_id, base)
        if character_dir is not None:
            return character_dir
    except Exception:  # noqa: BLE001 - 角色层失败不影响回退
        pass

    default_dir = Path(repo_root or Path(__file__).resolve().parents[1]) / "assets" / "animations"
    return default_dir if default_dir.is_dir() else None
