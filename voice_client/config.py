# -*- coding: utf-8 -*-
"""语音配置容错加载（插件管理版）。

两种模式：

1. **显式路径** ``load_voice_config(path=...)``：旧单文件语义，逐字保留
   （测试与高级用法依赖 ``defaults(missing)`` 等 source 口径）。
2. **默认** ``load_voice_config()``：插件管理链——
   环境变量 > 用户插件配置 > 旧配置文件 > 默认值；
   用户插件配置缺失时做**一次性迁移**（幂等、失败哑化，绝不影响启动）。

管理模式的默认值：``enabled=True``（语音能力默认存在——旧文件的
``enabled=false`` 是"语音像坏了"的根因，迁移时按新默认落盘），
``auto_play=False``（v1.3 设计：默认仅播放按钮）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("firefly.voice")

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "voice_config.yaml"

# -- 用户插件配置落点（<用户数据根>/plugins/firefly_voice/config.yaml） -------
USER_PLUGIN_DIR_NAME = "firefly_voice"
USER_CONFIG_NAME = "config.yaml"

# -- 环境变量覆盖（最高优先级） ----------------------------------------------
ENV_ENABLED = "FIREFLY_VOICE_ENABLED"
ENV_AUTO_PLAY = "FIREFLY_VOICE_AUTO_PLAY"
ENV_URL = "FIREFLY_VOICE_URL"

_TRUE_VALUES = {"1", "true", "on", "yes"}

# 显式路径模式默认值（旧语义, enabled=False, 供 defaults(missing) 口径）
_DEFAULTS: dict[str, Any] = {
    "voice": {
        "enabled": False,
        "auto_play": False,
        "server": {
            "url": "http://127.0.0.1:8300",
            "connect_timeout_s": 2.0,
            "read_timeout_s": 30.0,
        },
        "emotion": {"enabled": True},
        "max_speak_chars": 1600,
    }
}

# 管理模式默认值（语音能力默认存在）
_MANAGED_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "auto_play": False,
    "url": "http://127.0.0.1:8300",
    "connect_timeout_s": 2.0,
    "read_timeout_s": 30.0,
    "emotion_enabled": True,
    "max_speak_chars": 1600,
    # 服务管理路径：机器相关，默认空串（写入用户配置，不进仓库代码）
    "service_root": "",
    "service_python": "",
    "service_script": "api/server.py",
}


@dataclass
class VoiceConfig:
    enabled: bool = False
    auto_play: bool = False          # v1.3: False=回复后仅显示播放按钮, 用户点击才朗读
    url: str = "http://127.0.0.1:8300"
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 30.0
    emotion_enabled: bool = True
    max_speak_chars: int = 1600
    source: str = "defaults"
    raw: dict = field(default_factory=dict)
    # 语音服务管理（firefly_voice 插件使用；空串=未配置）
    service_root: str = ""
    service_python: str = ""
    service_script: str = "api/server.py"


# ---------------------------------------------------------------------------
# 管理模式：路径与读写
# ---------------------------------------------------------------------------

def managed_user_config_path() -> Path:
    """用户插件配置路径（尊重 FIREFLY_USER_DATA_DIR）。"""
    try:
        from core.user_paths import get_user_data_paths

        return get_user_data_paths().plugins / USER_PLUGIN_DIR_NAME / USER_CONFIG_NAME
    except Exception:  # noqa: BLE001 - 路径解析失败退回仓库旁路径, 只读仍可用
        return PROJECT_DIR / "config" / f"{USER_PLUGIN_DIR_NAME}.user.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    """best-effort 读 YAML；任何失败返回空 dict。"""
    try:
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                return loaded
            log.warning("voice 配置顶层不是映射: %s", path)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 voice 配置失败 (%s): %s", path, exc)
    return {}


def _flatten(data: dict[str, Any]) -> dict[str, Any]:
    """把 legacy/用户两种同形 YAML 展平成解析用键值。"""
    voice = data.get("voice") if isinstance(data.get("voice"), dict) else {}
    server = voice.get("server") if isinstance(voice.get("server"), dict) else {}
    emotion = voice.get("emotion") if isinstance(voice.get("emotion"), dict) else {}
    service = voice.get("service") if isinstance(voice.get("service"), dict) else {}
    flat: dict[str, Any] = {}
    for key, src, dst in (
        ("enabled", voice, "enabled"),
        ("auto_play", voice, "auto_play"),
        ("max_speak_chars", voice, "max_speak_chars"),
        ("url", server, "url"),
        ("connect_timeout_s", server, "connect_timeout_s"),
        ("read_timeout_s", server, "read_timeout_s"),
        ("enabled", emotion, "emotion_enabled"),
        ("root", service, "service_root"),
        ("python", service, "service_python"),
        ("script", service, "service_script"),
    ):
        if isinstance(src, dict) and key in src:
            flat[dst] = src[key]
    return flat


def _to_nested(flat: dict[str, Any]) -> dict[str, Any]:
    """把解析键值装回嵌套 YAML 形状（与 legacy 文件同形）。"""
    voice: dict[str, Any] = {
        "enabled": bool(flat.get("enabled", _MANAGED_DEFAULTS["enabled"])),
        "auto_play": bool(flat.get("auto_play", _MANAGED_DEFAULTS["auto_play"])),
        "max_speak_chars": int(flat.get("max_speak_chars", _MANAGED_DEFAULTS["max_speak_chars"])),
        "emotion": {"enabled": bool(flat.get("emotion_enabled", True))},
        "server": {
            "url": str(flat.get("url", _MANAGED_DEFAULTS["url"])),
            "connect_timeout_s": float(flat.get("connect_timeout_s", 2.0)),
            "read_timeout_s": float(flat.get("read_timeout_s", 30.0)),
        },
        "service": {
            "root": str(flat.get("service_root", "")),
            "python": str(flat.get("service_python", "")),
            "script": str(flat.get("service_script", "api/server.py")),
        },
    }
    return {"voice": voice}


def save_managed_overrides(updates: dict[str, Any]) -> bool:
    """把解析键值合并进用户插件配置（原子写；永不抛异常）。"""
    path = managed_user_config_path()
    try:
        current = _flatten(_read_yaml(path))
        current.update({k: v for k, v in updates.items() if v is not None})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(_to_nested(current), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001 - 写失败只告警, 不影响主程序
        log.warning("写入用户语音配置失败 (%s): %s", path, exc)
        return False


def _migrate_if_needed(user_path: Path, legacy_flat: dict[str, Any]) -> bool:
    """一次性迁移：用户配置缺失时以 legacy 为素材 + 新默认落盘。

    规则（有意决策）：enabled 按新默认 True（旧 false 即故障语义）；
    auto_play 保持 False（v1.3）；其余字段（url/超时/情绪/截断）抄旧文件。
    """
    if user_path.is_file():
        return False
    seed = dict(_MANAGED_DEFAULTS)
    seed["enabled"] = True          # 故障修复语义
    seed["auto_play"] = False       # v1.3 设计
    for key in ("url", "connect_timeout_s", "read_timeout_s",
                "emotion_enabled", "max_speak_chars"):
        if key in legacy_flat:
            seed[key] = legacy_flat[key]
    try:
        user_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = user_path.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(_to_nested(seed), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, user_path)
        log.info("voice 插件配置已迁移生成: %s", user_path)
        return True
    except Exception as exc:  # noqa: BLE001 - 迁移失败不影响只读解析
        log.warning("voice 配置迁移失败 (%s): %s", user_path, exc)
        return False


def _env_overrides() -> dict[str, Any]:
    out: dict[str, Any] = {}
    raw = os.environ.get(ENV_ENABLED)
    if raw is not None and raw.strip():
        out["enabled"] = raw.strip().lower() in _TRUE_VALUES
    raw = os.environ.get(ENV_AUTO_PLAY)
    if raw is not None and raw.strip():
        out["auto_play"] = raw.strip().lower() in _TRUE_VALUES
    raw = os.environ.get(ENV_URL)
    if raw is not None and raw.strip():
        out["url"] = raw.strip()
    return out


# 管理模式解析缓存（按两侧文件 mtime + 环境变量键失效）
_MANAGED_CACHE: dict[str, Any] = {"key": None, "config": None}


def _managed_cache_key(user_path: Path) -> tuple:
    def _mtime(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return -1

    return (
        str(user_path), _mtime(user_path),
        str(CONFIG_PATH), _mtime(CONFIG_PATH),
        os.environ.get(ENV_ENABLED, ""), os.environ.get(ENV_AUTO_PLAY, ""),
        os.environ.get(ENV_URL, ""),
    )


def _load_managed() -> VoiceConfig:
    user_path = managed_user_config_path()
    legacy_data = _read_yaml(CONFIG_PATH)
    legacy_flat = _flatten(legacy_data)

    migrated = _migrate_if_needed(user_path, legacy_flat)
    user_flat = _flatten(_read_yaml(user_path))

    merged = dict(_MANAGED_DEFAULTS)
    merged.update(legacy_flat)      # 旧文件次优先
    merged.update(user_flat)        # 用户插件配置优先
    env = _env_overrides()
    merged.update(env)              # 环境变量最高

    if env:
        source = "managed(env)"
    elif migrated:
        source = "managed(migrated)"
    elif user_flat:
        source = "managed(user)"
    else:
        source = "managed(defaults)"

    url = str(merged.get("url") or _MANAGED_DEFAULTS["url"]).rstrip("/")
    return VoiceConfig(
        enabled=bool(merged.get("enabled", True)),
        auto_play=bool(merged.get("auto_play", False)),
        url=url,
        connect_timeout_s=float(merged.get("connect_timeout_s", 2.0)),
        read_timeout_s=float(merged.get("read_timeout_s", 30.0)),
        emotion_enabled=bool(merged.get("emotion_enabled", True)),
        max_speak_chars=int(merged.get("max_speak_chars", 1600)),
        source=source,
        raw=merged,
        service_root=str(merged.get("service_root") or ""),
        service_python=str(merged.get("service_python") or ""),
        service_script=str(merged.get("service_script") or "api/server.py"),
    )


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

def load_voice_config(path: Path | None = None) -> VoiceConfig:
    """加载语音配置。

    ``path`` 给出时 = 旧单文件语义（缺文件/坏文件回默认值并告警）；
    否则 = 插件管理链（环境变量 > 用户插件配置 > 旧配置文件 > 默认值）。
    """
    if path is None:
        try:
            key = _managed_cache_key(managed_user_config_path())
            if _MANAGED_CACHE["key"] == key and _MANAGED_CACHE["config"] is not None:
                return _MANAGED_CACHE["config"]
            config = _load_managed()
            _MANAGED_CACHE["key"] = key
            _MANAGED_CACHE["config"] = config
            return config
        except Exception as exc:  # noqa: BLE001 - 管理链任何失败回退旧语义
            log.warning("voice 管理配置解析失败 (%s), 回退默认", exc)
            path = None
            return _load_explicit(CONFIG_PATH)

    return _load_explicit(path)


def _load_explicit(cfg_path: Path) -> VoiceConfig:
    """旧单文件语义（逐字保留, 显式路径与回退路径共用）。"""
    data: dict[str, Any] = {}
    try:
        if cfg_path.is_file():
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
                source = str(cfg_path)
            else:
                log.warning("voice_config.yaml 顶层不是映射, 使用默认配置")
                source = "defaults(bad-yaml)"
        else:
            source = "defaults(missing)"
    except Exception as exc:  # noqa: BLE001 — 配置读取失败不属于致命错误
        log.warning("读取 voice_config.yaml 失败 (%s), 使用默认配置", exc)
        source = "defaults(error)"

    voice = _DEFAULTS["voice"] | (data.get("voice") or {})
    server = _DEFAULTS["voice"]["server"] | (voice.get("server") or {})
    emotion = _DEFAULTS["voice"]["emotion"] | (voice.get("emotion") or {})
    return VoiceConfig(
        enabled=bool(voice.get("enabled", False)),
        auto_play=bool(voice.get("auto_play", False)),
        url=str(server.get("url", _DEFAULTS["voice"]["server"]["url"])).rstrip("/"),
        connect_timeout_s=float(server.get("connect_timeout_s", 2.0)),
        read_timeout_s=float(server.get("read_timeout_s", 30.0)),
        emotion_enabled=bool(emotion.get("enabled", True)),
        max_speak_chars=int(voice.get("max_speak_chars", 600)),
        source=source,
        raw=data,
    )
