"""JSONL subprocess client for the standalone Firefly_BiliInsight_Service.

The BiliInsight service lives outside this repository (GPL-3.0) and is
talked to strictly through a subprocess stdin/stdout JSONL boundary — no
HTTP, no in-process import, so no GPL code is embedded in Firefly.

Wire protocol (one request line in, one response line out, UTF-8):
    request : {"action": "...", ...params}
    response: {"action", "ok", "data", "error", "latency_ms",
               "service", "version"}

Service-side errors arrive as ``error.type`` in
{env_dependency, model, bilibili_download, asr, invalid_args,
unknown_action, internal}. Client-side failure conditions (timeout,
crashed child, unparseable output, missing installation) are normalized
into the extra kinds {timeout, crashed, protocol, env_dependency} so
callers only ever have to catch :class:`BiliServiceError`.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.path_config import (
    BILI_INSIGHT_ROOT_ENV,
    LEGACY_BILI_ROOT_ENV,
    load_path_config,
)

DEFAULT_SERVICE_DIR = load_path_config().bili_insight_root

DEFAULT_ACTION_TIMEOUTS: dict[str, float] = {
    "health": 30.0,
    "metadata": 120.0,
    "frame": 180.0,
    "transcribe": 900.0,  # cold local ASR of a long video is minutes
}

_ERROR_TAIL_CHARS = 400


class BiliServiceError(Exception):
    """Unified failure of one BiliInsight service call."""

    def __init__(self, kind: str, message: str, action: str | None = None) -> None:
        self.kind = str(kind)
        self.action = action
        prefix = f"[{action}:{kind}] " if action else f"[{kind}] "
        super().__init__(prefix + message)


def _service_dir(service_dir: Path | str | None) -> Path:
    if service_dir is not None:
        return Path(service_dir)
    configured = os.environ.get(BILI_INSIGHT_ROOT_ENV) or os.environ.get(
        LEGACY_BILI_ROOT_ENV
    )
    return Path(configured) if configured else DEFAULT_SERVICE_DIR


def _python_exe(service_dir: Path) -> Path:
    override = os.environ.get("FIREFLY_BILI_SERVICE_PYTHON")
    if override:
        return Path(override)
    return service_dir / ".venv" / "Scripts" / "python.exe"


@dataclass(frozen=True)
class BiliInsightClient:
    """One call = one child process; the service is stateless per line."""

    service_dir: Path | str | None = None
    python_exe: Path | str | None = None
    timeouts: dict[str, float] | None = None

    # ------------------------------------------------------------ public API

    def health(self) -> dict:
        return self.call("health")

    def metadata(self, video_id: str) -> dict:
        return self.call("metadata", {"video_id": video_id})

    def transcribe(
        self,
        video_id: str,
        *,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> dict:
        payload: dict[str, object] = {"video_id": video_id}
        if start_time is not None:
            payload["start_time"] = float(start_time)
        if end_time is not None:
            payload["end_time"] = float(end_time)
        return self.call("transcribe", payload)

    def frame(self, video_id: str, timestamp: str | float, *, include_base64: bool = False) -> dict:
        return self.call("frame", {
            "video_id": video_id,
            "timestamp": str(timestamp),
            "include_base64": bool(include_base64),
        })

    def call(
        self,
        action: str,
        payload: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        """Run one JSONL action and return ``data``; raise BiliServiceError otherwise."""
        directory = _service_dir(self.service_dir)
        script = directory / "service.py"
        python = Path(self.python_exe) if self.python_exe else _python_exe(directory)
        if not script.is_file():
            raise BiliServiceError("env_dependency", f"service.py not found: {script}", action)
        if not python.is_file():
            raise BiliServiceError("env_dependency", f"service python not found: {python}", action)

        request = {"action": action}
        if payload:
            request.update(payload)
        effective_timeout = float(
            timeout
            if timeout is not None
            else (self.timeouts or DEFAULT_ACTION_TIMEOUTS).get(action, 120.0)
        )

        child_env = dict(os.environ)
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                [str(python), "-X", "utf8", str(script)],
                input=json.dumps(request, ensure_ascii=False) + "\n",
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                env=child_env,
                cwd=str(directory),
            )
        except subprocess.TimeoutExpired as exc:
            raise BiliServiceError(
                "timeout",
                f"no response within {effective_timeout:.0f}s",
                action,
            ) from exc
        except OSError as exc:
            raise BiliServiceError("env_dependency", f"failed to launch service: {exc}", action)

        envelope = self._parse_envelope(action, completed.stdout)
        if envelope is None:
            stderr_tail = (completed.stderr or "")[-_ERROR_TAIL_CHARS:].strip()
            raise BiliServiceError(
                "crashed",
                f"exit={completed.returncode}, no JSONL response"
                + (f"; stderr: {stderr_tail}" if stderr_tail else ""),
                action,
            )
        if not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise BiliServiceError(
                str(error.get("type") or "internal"),
                str(error.get("message") or "unknown service error"),
                action,
            )
        return envelope.get("data") or {}

    # ------------------------------------------------------------ internals

    @staticmethod
    def _parse_envelope(action: str, stdout: str) -> dict | None:
        """Return the last parseable JSONL envelope, or None when none exists."""
        envelope = None
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "ok" in candidate:
                envelope = candidate
        return envelope


__all__ = ["BiliInsightClient", "BiliServiceError", "DEFAULT_SERVICE_DIR"]
