"""Schema and path rules for Firefly Character Package v1.0."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

SCHEMA_VERSION = "1.0"
REQUIRED_ROOT_FILES = frozenset({"character.json", "preview.png", "metadata.json"})
CHARACTER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object contains the same key more than once."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_document(raw: bytes, label: str) -> Any:
    """Decode a strict UTF-8 JSON document and reject duplicate keys."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc


def path_safety_error(path: str, *, allow_directory: bool = False) -> str | None:
    """Return a reason when an archive path is unsafe on Windows."""

    if not isinstance(path, str) or not path:
        return "path is empty"
    if "\x00" in path:
        return "path contains a NUL byte"
    if "\\" in path:
        return "path must use forward slashes"

    is_directory = path.endswith("/")
    candidate = path[:-1] if is_directory else path
    if is_directory and not allow_directory:
        return "directory entry is not allowed here"
    if not candidate:
        return "root directory entry is not allowed"
    if candidate.startswith("/") or candidate.startswith("//"):
        return "absolute path is not allowed"

    raw_parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return "empty or dot path segments are not allowed"

    pure = PurePosixPath(candidate)
    if pure.is_absolute():
        return "absolute path is not allowed"

    for part in raw_parts:
        if ":" in part:
            return "drive or alternate data stream syntax is not allowed"
        if part.endswith((" ", ".")):
            return "path segment may not end with a space or dot"
        stem = part.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            return f"Windows reserved name is not allowed: {part}"
    return None


def validate_manifest(manifest: Any) -> list[str]:
    """Validate character.json and return all human-readable errors."""

    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["character.json root must be an object"]

    required = {
        "schema_version",
        "character_id",
        "display_name",
        "author",
        "description",
        "version",
        "animations",
        "default_animation",
        "size",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        errors.append(f"character.json missing fields: {', '.join(missing)}")

    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")

    character_id = manifest.get("character_id")
    if not isinstance(character_id, str) or not CHARACTER_ID_PATTERN.fullmatch(character_id):
        errors.append("character_id must match ^[a-z0-9][a-z0-9_-]{0,63}$")

    for field in ("display_name", "version"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be a non-empty string")
    for field in ("author", "description"):
        if not isinstance(manifest.get(field), str):
            errors.append(f"{field} must be a string")

    size = manifest.get("size")
    if not isinstance(size, dict):
        errors.append("size must be an object")
    else:
        for dimension in ("width", "height"):
            value = size.get(dimension)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4096:
                errors.append(f"size.{dimension} must be an integer from 1 to 4096")

    animations = manifest.get("animations")
    if not isinstance(animations, dict) or not animations:
        errors.append("animations must be a non-empty object")
        animations = {}

    seen_targets: set[str] = set()
    for action, path in animations.items():
        if not isinstance(action, str) or not ACTION_PATTERN.fullmatch(action):
            errors.append(f"invalid action name: {action!r}")
        if not isinstance(path, str):
            errors.append(f"animation path for {action!r} must be a string")
            continue
        safety_error = path_safety_error(path)
        if safety_error:
            errors.append(f"unsafe animation path {path!r}: {safety_error}")
            continue
        pure = PurePosixPath(path)
        if len(pure.parts) != 2 or pure.parts[0] != "animations" or pure.suffix.lower() != ".gif":
            errors.append(f"animation path must be animations/<name>.gif: {path!r}")
        folded = path.casefold()
        if folded in seen_targets:
            errors.append(f"duplicate animation target: {path!r}")
        seen_targets.add(folded)

    default_animation = manifest.get("default_animation")
    if not isinstance(default_animation, str) or default_animation not in animations:
        errors.append("default_animation must name an entry in animations")
    return errors


def declared_package_files(manifest: dict[str, Any]) -> set[str]:
    """Return the exact file allowlist declared by a valid manifest."""

    animations = manifest.get("animations", {})
    return set(REQUIRED_ROOT_FILES) | set(animations.values())
