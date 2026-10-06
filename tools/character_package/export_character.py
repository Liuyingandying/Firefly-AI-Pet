"""Convert an existing Character Studio asset directory into a package."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from character_package.packer import pack_character  # type: ignore[import-not-found]
    from character_package.schema import ACTION_PATTERN, CHARACTER_ID_PATTERN, SCHEMA_VERSION  # type: ignore[import-not-found]
else:
    from .packer import pack_character
    from .schema import ACTION_PATTERN, CHARACTER_ID_PATTERN, SCHEMA_VERSION

ABSOLUTE_PATH_PATTERN = re.compile(r"(?:^|[\s\"'])(?:[a-zA-Z]:[\\/]|\\\\|/)")


def _safe_public_scalar(value: Any) -> str | int | float | bool | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    if isinstance(value, str) and ABSOLUTE_PATH_PATTERN.search(value):
        return None
    return value


def _read_studio_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Studio metadata: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Studio metadata root must be an object")
    return value


def _public_metadata(source: dict[str, Any], character_id: str, version: str, action_names: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "metadata_version": "1.0",
        "character": character_id,
        "version": version,
        "generator": _safe_public_scalar(source.get("generator")) or "IP-Adapter + AnimateDiff-Lightning",
        "size": "",
        "actions": {},
    }
    for optional in ("created_time",):
        safe_value = _safe_public_scalar(source.get(optional))
        if safe_value is not None:
            result[optional] = safe_value

    source_actions = source.get("actions") if isinstance(source.get("actions"), dict) else {}
    for action in action_names:
        public_action: dict[str, Any] = {}
        source_action = source_actions.get(action) if isinstance(source_actions.get(action), dict) else {}
        for key in ("action_prompt", "created_time", "total_seconds", "peak_vram_mib"):
            safe_value = _safe_public_scalar(source_action.get(key))
            if safe_value is not None:
                public_action[key] = safe_value
        result["actions"][action] = public_action
    return result


def export_character(
    studio_asset_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    character_id: str | None = None,
    display_name: str | None = None,
    default_animation: str | None = None,
    author: str = "",
    description: str = "",
    version: str = "1.0",
) -> Path:
    source = Path(studio_asset_dir).resolve()
    if not source.is_dir():
        raise ValueError(f"Studio asset directory does not exist: {source}")
    resolved_id = character_id or source.name.lower().replace(" ", "_")
    if not CHARACTER_ID_PATTERN.fullmatch(resolved_id):
        raise ValueError("character_id must match ^[a-z0-9][a-z0-9_-]{0,63}$")

    gifs = sorted(source.glob("*.gif"), key=lambda path: (path.stem.casefold(), path.stem))
    if not gifs:
        raise ValueError("Studio asset directory contains no root-level GIF animations")
    actions: dict[str, Path] = {}
    for gif in gifs:
        action = gif.stem
        if not ACTION_PATTERN.fullmatch(action):
            raise ValueError(f"invalid action filename: {gif.name}")
        actions[action] = gif

    if default_animation is None:
        if "idle" not in actions:
            raise ValueError("--default-animation is required when the Studio asset has no idle.gif")
        default_animation = "idle"
    if default_animation not in actions:
        raise ValueError(f"default animation does not exist: {default_animation}")

    sizes: set[tuple[int, int]] = set()
    frame_counts: dict[str, int] = {}
    for action, gif in actions.items():
        with Image.open(gif) as image:
            if image.format != "GIF":
                raise ValueError(f"not a GIF: {gif}")
            sizes.add(image.size)
            frame_counts[action] = getattr(image, "n_frames", 1)
    if len(sizes) != 1:
        raise ValueError("Studio animations do not share one size")
    width, height = next(iter(sizes))

    metadata_source = _read_studio_metadata(source / "metadata.json")
    metadata = _public_metadata(metadata_source, resolved_id, version, list(actions))
    metadata["size"] = f"{width}x{height}"
    for action, frames in frame_counts.items():
        metadata["actions"][action]["frames"] = frames

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "character_id": resolved_id,
        "display_name": display_name or resolved_id.replace("_", " ").title(),
        "author": author,
        "description": description,
        "version": version,
        "animations": {action: f"animations/{action}.gif" for action in actions},
        "default_animation": default_animation,
        "size": {"width": width, "height": height},
    }
    output = Path(output_path).resolve() if output_path else Path.cwd() / f"{resolved_id}.character"

    with tempfile.TemporaryDirectory(prefix="firefly-character-export-") as temporary_name:
        staging = Path(temporary_name)
        animations_dir = staging / "animations"
        animations_dir.mkdir()
        for action, gif in actions.items():
            shutil.copyfile(gif, animations_dir / f"{action}.gif")
        with Image.open(actions[default_animation]) as image:
            image.seek(0)
            image.convert("RGBA").save(staging / "preview.png", format="PNG")
        (staging / "character.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        (staging / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        return pack_character(staging, output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Character Studio assets as a .character package.")
    parser.add_argument("--input", required=True, type=Path, dest="studio_asset_dir")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--character-id")
    parser.add_argument("--display-name")
    parser.add_argument("--default-animation")
    parser.add_argument("--author", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--version", default="1.0")
    args = parser.parse_args(argv)
    try:
        output = export_character(
            args.studio_asset_dir,
            args.output,
            character_id=args.character_id,
            display_name=args.display_name,
            default_animation=args.default_animation,
            author=args.author,
            description=args.description,
            version=args.version,
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
