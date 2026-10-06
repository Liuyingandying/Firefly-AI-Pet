"""Firefly Skin System M1 — metadata schema (protocol v1.0, pure data).

Skin = character visual layer (animations/theme/preview), zero executable code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
SKIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VALID_STATES = ("idle", "running", "review", "waiting", "waving", "failed",
                "jumping", "sleeping", "talking")
DEFAULT_FALLBACK_STATE = "idle"


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class SkinManifest:
    schema_version: int = SCHEMA_VERSION
    skin_id: str = ""
    display_name: str = ""
    author: str = ""
    version: str = "0.0.0"
    description: str = ""
    compatible_characters: tuple[str, ...] = ()
    states: tuple[str, ...] = (
        "idle", "running", "review", "waiting", "waving", "failed", "jumping"
    )
    fallback_state: str = DEFAULT_FALLBACK_STATE
    frame_hint: dict = field(default_factory=dict)
    theme: dict = field(default_factory=dict)
    license: str = ""
    tags: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SchemaError(f"schema_version 不支持: {self.schema_version}")
        if not SKIN_ID_RE.match(self.skin_id):
            raise SchemaError(f"skin_id 非法: {self.skin_id!r}")
        if not self.display_name.strip():
            raise SchemaError("display_name 缺失")
        unknown = set(self.states) - set(VALID_STATES)
        if unknown:
            raise SchemaError(f"animations.states 含未知状态: {sorted(unknown)}")
        theme = self.theme
        if theme:
            glow = theme.get("glow_color")
            if glow is not None and (
                not isinstance(glow, (list, tuple)) or len(glow) != 3
                or not all(isinstance(v, int) and 0 <= v <= 255 for v in glow)
            ):
                raise SchemaError(f"theme.glow_color 非法: {glow!r}")


def parse_manifest(data: Any) -> SkinManifest:
    """Raw JSON -> SkinManifest, raising SchemaError on violation."""
    if not isinstance(data, dict):
        raise SchemaError("metadata.json 顶层必须是对象")
    try:
        manifest = SkinManifest(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            skin_id=str(data.get("skin_id") or "").strip(),
            display_name=str(data.get("display_name") or "").strip(),
            author=str(data.get("author") or "").strip(),
            version=str(data.get("version") or "0.0.0").strip(),
            description=str(data.get("description") or "").strip(),
            compatible_characters=tuple(
                str(c) for c in data.get("compatible_characters") or ()
            ),
            states=tuple(str(s) for s in (data.get("animations") or {}).get("states")
                         or ("idle", "running", "review", "waiting",
                             "waving", "failed", "jumping")),
            fallback_state=str(
                (data.get("animations") or {}).get("fallback_state")
                or DEFAULT_FALLBACK_STATE
            ),
            frame_hint=dict((data.get("animations") or {}).get("frame_hint") or {}),
            theme=dict(data.get("theme") or {}),
            license=str(data.get("license") or "").strip(),
            tags=tuple(str(t) for t in data.get("tags") or ()),
        )
        manifest.validate()
        return manifest
    except (TypeError, ValueError) as exc:
        raise SchemaError(str(exc)) from exc