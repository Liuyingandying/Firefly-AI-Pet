"""Context Budget for Interactive Learning (v0.1.1, Phase 3).

Official source: Z Code's own ``model_usage`` table records the real input
tokens of every model request. The LATEST request's input_tokens is the best
stable proxy for "how full this session's context is right now" (it contains
system + skill + MCP schemas + the whole conversation so far).

Budget states (thresholds against the context limit, default 200k —
reverse-engineered from Z Code's own percentage display: a session peaking
at 220,870 tokens showed as ~111%):
    < 70%  NORMAL
    70-85% WARNING
    > 85%  ROLLOVER_REQUIRED

teach-mcp remains the only learning-fact authority: this module measures UI
capacity, never learning state.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from learning.diagnostics import log_marker

DEFAULT_CONTEXT_LIMIT = 200_000
NORMAL, WARNING, ROLLOVER_REQUIRED = "NORMAL", "WARNING", "ROLLOVER_REQUIRED"
WARNING_RATIO = 0.70
ROLLOVER_RATIO = 0.85


def session_db() -> Path:
    return Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def context_limit() -> int:
    override = os.environ.get("FIREFLY_CONTEXT_LIMIT", "").strip()
    if override.isdigit():
        return int(override)
    return DEFAULT_CONTEXT_LIMIT


def latest_context_usage(zcode_session_id: str) -> int | None:
    """Latest model request input_tokens for this Z Code session (official).

    Returns None when the session is unknown / db unreadable — callers must
    treat None as "cannot measure" and stay on the current session.
    """
    if not zcode_session_id:
        return None
    db = session_db()
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT input_tokens FROM model_usage WHERE session_id = ?"
                " ORDER BY started_at DESC LIMIT 1",
                (zcode_session_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return int(row[0])


def budget_state(usage: int | None, limit: int | None = None) -> str:
    limit = limit or context_limit()
    if usage is None or limit <= 0:
        return NORMAL  # cannot measure -> no forced rollover
    ratio = usage / limit
    if ratio > ROLLOVER_RATIO:
        return ROLLOVER_REQUIRED
    if ratio >= WARNING_RATIO:
        return WARNING
    return NORMAL
