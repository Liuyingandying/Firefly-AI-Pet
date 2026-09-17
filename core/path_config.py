"""Small, fail-safe resolver for optional external capability roots.

Priority is intentionally fixed and narrow:
1. environment variable
2. ``config/path_config.yaml``
3. portable default below Firefly's per-user data root

Missing or malformed configuration never prevents Firefly from starting.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from core.user_paths import UserDataPaths, get_user_data_paths


log = logging.getLogger("firefly.path_config")
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "path_config.yaml"

PLUGIN_ROOT_ENV = "FIREFLY_PLUGIN_ROOT"
BILI_INSIGHT_ROOT_ENV = "FIREFLY_BILI_INSIGHT_ROOT"
LEGACY_BILI_ROOT_ENV = "FIREFLY_BILI_SERVICE_DIR"


@dataclass(frozen=True, slots=True)
class ExternalPathConfig:
    plugin_root: Path
    bili_insight_root: Path
    source: str = "defaults"


def load_path_config(
    path: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    user_paths: UserDataPaths | None = None,
) -> ExternalPathConfig:
    """Resolve optional external roots without requiring either to exist."""

    environment = os.environ if environ is None else environ
    paths = user_paths or get_user_data_paths()
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    configured: dict[str, object] = {}
    source = "defaults"
    try:
        if config_path.is_file():
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            values = raw.get("paths") if isinstance(raw, dict) else None
            if isinstance(values, dict):
                configured = values
                source = str(config_path)
            else:
                source = "defaults(bad-schema)"
    except Exception as exc:  # noqa: BLE001 - optional config must fail safe
        log.warning("path config unavailable; using portable defaults (%s)", exc)
        source = "defaults(error)"

    plugin_root = _resolve_root(
        environment.get(PLUGIN_ROOT_ENV),
        configured.get("plugin_root"),
        paths.plugins,
        project_dir=PROJECT_DIR,
    )
    bili_env = environment.get(BILI_INSIGHT_ROOT_ENV) or environment.get(
        LEGACY_BILI_ROOT_ENV
    )
    bili_root = _resolve_root(
        bili_env,
        configured.get("bili_insight_root"),
        paths.plugins / "bili-insight",
        project_dir=PROJECT_DIR,
    )
    return ExternalPathConfig(plugin_root, bili_root, source)


def _resolve_root(
    environment_value: object,
    configured_value: object,
    default: Path,
    *,
    project_dir: Path,
) -> Path:
    for value in (environment_value, configured_value):
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = project_dir / candidate
        return candidate
    return Path(default)


__all__ = [
    "BILI_INSIGHT_ROOT_ENV",
    "DEFAULT_CONFIG_PATH",
    "ExternalPathConfig",
    "LEGACY_BILI_ROOT_ENV",
    "PLUGIN_ROOT_ENV",
    "load_path_config",
]
