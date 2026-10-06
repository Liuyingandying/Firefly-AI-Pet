"""Firefly Skin System M1 — 纯数据皮肤协议（零代码执行面）。"""

from skins.installer import install_skin, uninstall_skin
from skins.registry import (
    get_skin,
    list_skins,
    load_active_skin,
    save_active_skin,
)
from skins.resolver import animation_resolver
from skins.schema import SkinManifest, parse_manifest
from skins.validator import validate_skin

__all__ = [
    "SkinManifest",
    "animation_resolver",
    "get_skin",
    "install_skin",
    "list_skins",
    "load_active_skin",
    "parse_manifest",
    "save_active_skin",
    "uninstall_skin",
    "validate_skin",
]
