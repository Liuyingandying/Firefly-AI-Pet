"""Shared atomic-JSON storage helpers for the Learning Bridge.

Every bridge artifact (manifest, context, binding, markers) is written
atomically: tmp file + os.replace, so a crash mid-write can never leave a
half-written protocol file behind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class BridgeFileError(Exception):
    """Structured error for unreadable / invalid bridge protocol files."""

    def __init__(self, code: str, message: str, path: Path | str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = str(path) if path is not None else None

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


def read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BridgeFileError("FILE_NOT_FOUND", f"文件不存在: {path}", path) from exc
    except OSError as exc:
        raise BridgeFileError("FILE_UNREADABLE", f"文件无法读取: {path}: {exc}", path) from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise BridgeFileError(
            "FILE_MALFORMED", f"文件不是合法 JSON: {path}: {exc}", path
        ) from exc


def atomic_write_json(path: Path, payload: Any) -> Path:
    """Write ``payload`` as UTF-8 JSON, atomically replacing ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
