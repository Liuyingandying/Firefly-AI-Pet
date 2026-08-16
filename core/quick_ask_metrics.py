"""Qt-free latency telemetry for Firefly Short Ask / Quick Ask turns.

Records structured milestones (T0–T6) and derives diagnostic timings. The
payload is strictly allowlisted for diagnostics; it never contains the prompt,
the response, API keys, auth tokens, environment variables, or a full native
session id (only a short hash prefix). Persistence is a transient latest file
plus a bounded ring buffer — no database, no unbounded log growth.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
METRICS_DIR = PROJECT_DIR / "runtime" / "metrics"
LATEST_FILE = METRICS_DIR / "quick_ask_latest.json"
HISTORY_FILE = METRICS_DIR / "quick_ask_history.json"
RING_BUFFER_SIZE = 20

# Safe duration fields always present (may be None if the milestone never fired).
DERIVED_FIELDS = (
    "dispatch_ms",
    "startup_ms",
    "first_event_ms",
    "first_text_ms",
    "total_ms",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def session_hash_prefix(session_id: str | None, length: int = 8) -> str | None:
    """Short, stable, non-reversible identifier for a native session id."""
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:length]


def workspace_token(workspace: Path | str) -> str:
    """Short diagnostic token for a workspace: last path segment, lowercased."""
    name = Path(workspace).name or str(Path(workspace)).replace(os.sep, "_")
    return name.lower()[:40]


def error_category(message: str | None) -> str | None:
    if not message:
        return None
    lowered = message.lower()
    if "permission" in lowered or "approval" in lowered:
        return "permission"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "reconnect" in lowered or "network" in lowered:
        return "network"
    if "not found" in lowered or "找不到" in lowered:
        return "missing_cli"
    return "other"


@dataclass
class AskTelemetry:
    """One turn's diagnostics. All timestamps are wall-clock ms (monotonic-ish)."""

    agent: str
    workspace: str
    created_at: int
    session_exists: bool = False
    session_hash: str | None = None
    # session_source: "new" | "memory" | "persisted" (provenance only).
    session_source: str | None = None
    resume_attempted: bool = False
    resume_fallback: bool = False
    isolated: bool = False
    milestone_t0: int | None = None
    milestone_t1: int | None = None
    milestone_t2: int | None = None
    milestone_t3: int | None = None
    milestone_t4: int | None = None
    milestone_t5: int | None = None
    milestone_t6: int | None = None
    cancel_requested: bool = False
    process_exit: int | None = None
    error_category: str | None = None

    def dispatch_ms(self) -> int | None:
        if self.milestone_t0 is not None and self.milestone_t6 is not None:
            return self.milestone_t6 - self.milestone_t0
        return None

    def startup_ms(self) -> int | None:
        if self.milestone_t0 is not None and self.milestone_t1 is not None:
            return self.milestone_t1 - self.milestone_t0
        return None

    def first_event_ms(self) -> int | None:
        if self.milestone_t0 is not None and self.milestone_t2 is not None:
            return self.milestone_t2 - self.milestone_t0
        return None

    def first_text_ms(self) -> int | None:
        if self.milestone_t0 is not None and self.milestone_t5 is not None:
            return self.milestone_t5 - self.milestone_t0
        return None

    def total_ms(self) -> int | None:
        return self.dispatch_ms()

    def to_payload(self) -> dict:
        payload = asdict(self)
        payload["derived"] = {
            "dispatch_ms": self.dispatch_ms(),
            "startup_ms": self.startup_ms(),
            "first_event_ms": self.first_event_ms(),
            "first_text_ms": self.first_text_ms(),
            "total_ms": self.total_ms(),
        }
        return payload

    def validate_safe(self) -> list[str]:
        """Return a list of problems if any sensitive value leaked into payload.

        Guards against accidental prompt/response/key capture. The payload must
        contain only primitives (int/str/bool/None) and the fixed field names.
        """
        problems: list[str] = []
        payload = self.to_payload()
        for key, value in payload.items():
            if not isinstance(value, (str, int, bool, float, type(None), list, dict)):
                problems.append(f"non-serializable field: {key}")
        if self.session_hash and len(self.session_hash) > 12:
            problems.append("session_hash too long")
        if self.session_hash and not all(c in "0123456789abcdef" for c in self.session_hash):
            problems.append("session_hash not a hex prefix")
        for sensitive in ("prompt", "response", "api_key", "token", "env", "password"):
            for key in payload:
                if sensitive in str(key).lower():
                    problems.append(f"sensitive key present: {key}")
        return problems


class MetricsWriter:
    """Persist telemetry to runtime/metrics as latest + bounded ring buffer."""

    def __init__(
        self,
        metrics_dir: Path | str = METRICS_DIR,
        *,
        ring_size: int = RING_BUFFER_SIZE,
    ) -> None:
        self.metrics_dir = Path(metrics_dir)
        self.latest_file = self.metrics_dir / "quick_ask_latest.json"
        self.history_file = self.metrics_dir / "quick_ask_history.json"
        self.ring_size = max(1, ring_size)

    def record(self, telemetry: AskTelemetry) -> dict:
        payload = telemetry.to_payload()
        try:
            self.metrics_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(self.latest_file, payload)
            history = self._read_history()
            history.append(payload)
            history = history[-self.ring_size:]
            self._write_json(self.history_file, history)
        except OSError:
            # Telemetry must never break a turn.
            pass
        return payload

    @staticmethod
    def _write_json(path: Path, value) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _read_history(self) -> list[dict]:
        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []
