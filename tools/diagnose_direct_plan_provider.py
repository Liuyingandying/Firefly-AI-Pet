"""Phase 9D.6-H3 — Direct Provider Diagnostic (read-only, single-shot).

Purpose
-------
The managed Workflow Claude Plan step, after every CLI-flag fix (H2 model pin,
H3 read-only tools, H4 effort omit), still gets the stable response:

    "I don't see a specific task or request..."

This script isolates the failure to one of two layers by removing the Claude
Code CLI entirely and talking to the provider HTTP API directly:

  * CASE A — the direct provider response correctly discusses the task and the
    plan validates => the Claude Code *compatibility layer* is the blocker.
  * CASE B — the direct provider response still says "no task" and the plan is
    invalid => the *provider / proxy / model itself* is degraded or
    incompatible.

Guarantees
----------
* Does NOT modify ``core/``, ``ui/``, ``app.py``, ``~/.claude/settings.json``,
  ``~/.codex/config.toml``, hooks, permissions, sandbox, or trust.
* Does NOT start the Claude Code CLI, QuickAskRunner, or ``run_cli.ps1``.
* Makes exactly ONE real provider HTTP call, with no retry.
* Reads the current Claude config (base URL / auth / model mapping) read-only.
* Never prints the API key/token, the authorization header value, or the full
  user config, and never writes any credential to a file.
* Codex is untouched (Codex online = 0 in this diagnostic).

The wire model is derived from the local mapping, not guessed: the workflow
passes ``--model opus``, which Claude Code resolves through
``ANTHROPIC_DEFAULT_OPUS_MODEL``. Claude Code keeps a ``[1M]`` long-context
beta suffix in that setting but strips it on the wire (verified against the
cc-switch proxy request log, whose ``request_model`` is the suffix-free id,
e.g. ``claude-opus-4-7``). This script strips the same suffix so the request
hits the proxy's model mapping exactly as the CLI's would.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Import the production contracts (all Qt-free / stdlib-only) without touching
# any other production module. These are the same callables the Workflow Plan
# executor uses.
PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.plan_validation import validate_plan_text  # noqa: E402
from core.routing_models import TaskRequest  # noqa: E402
from core.workflow_prompt import build_plan_prompt  # noqa: E402

# Fixed task from the diagnostic spec.
TASK_TEXT = "Fix greeting.py so the existing test passes.\nDo not modify test_greeting.py."

# The Workflow Plan step's model tier (same constant the executor passes as
# ``model=WORKFLOW_CLAUDE_MODEL``).
WORKFLOW_MODEL_TIER = "opus"

# Anthropic Messages API path appended to the configured base URL.
MESSAGES_PATH = "/v1/messages"

# Generous output budget so a valid plan is not truncated before it can name
# the task's files. Well under the proxy's non-streaming timeout budget.
MAX_TOKENS = 4096

# Match the cc-switch proxy's non-streaming timeout so a slow-but-valid model
# is not cut off by the client.
HTTP_TIMEOUT_SECONDS = 600


def _load_claude_env() -> dict[str, str]:
    """Read the current Claude config's ``env`` block (read-only).

    Mirrors the same source ``QuickAskRunner`` uses for isolated child
    processes. Returns {} if the file is missing or malformed.
    """
    settings_path = Path(os.path.expanduser("~")) / ".claude" / "settings.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items() if isinstance(v, (str, int, float))}


def _strip_context_suffix(model: str) -> str:
    """Remove the ``[1M]`` long-context beta suffix Claude Code strips on the wire."""
    return re.sub(r"\[\d+[mM]\]\s*$", "", (model or "")).strip()


def _redact(token: str) -> str:
    """Non-secret fingerprint of a credential for logging only."""
    if not token:
        return "<empty>"
    if len(token) <= 6:
        return "<redacted>"
    return f"{token[:2]}…{token[-2:]} (len={len(token)})"


def _extract_text(response_json: dict) -> str:
    """Join the text blocks of an Anthropic Messages API response."""
    content = response_json.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def main() -> int:
    env = _load_claude_env()

    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()

    # Strong-model mapping: "opus" tier -> ANTHROPIC_DEFAULT_OPUS_MODEL, with
    # the upstream real model recorded for the report. Derived from config, not
    # from memory.
    opus_model = (env.get("ANTHROPIC_DEFAULT_OPUS_MODEL") or "").strip()
    upstream_model = (env.get("ANTHROPIC_DEFAULT_OPUS_MODEL_NAME") or "").strip()
    wire_model = _strip_context_suffix(opus_model) or upstream_model or (env.get("ANTHROPIC_MODEL") or "").strip()

    # Production prompt, byte-for-byte the same wrapper the Plan executor uses.
    request = TaskRequest(text=TASK_TEXT)
    prompt = build_plan_prompt(request)

    report: dict = {
        "transport": "direct HTTP POST (urllib) — no claude CLI, no QuickAskRunner, no run_cli.ps1",
        "base_url": base_url or "<none>",
        "endpoint": f"{base_url}{MESSAGES_PATH}" if base_url else "<none>",
        "model_tier": WORKFLOW_MODEL_TIER,
        "wire_model": wire_model or "<none>",
        "upstream_model": upstream_model or "<none>",
        "auth": f"credential loaded from ANTHROPIC_AUTH_TOKEN ({_redact(token)}); value hidden",
        "prompt_length": len(prompt),
        "prompt_source": "build_plan_prompt(TaskRequest(TASK_TEXT))",
    }

    if not base_url or not token or not wire_model:
        report["http_success"] = False
        report["http_status"] = None
        report["http_error"] = (
            "missing config: "
            + ", ".join(
                k
                for k, v in (
                    ("base_url", base_url),
                    ("auth_token", token),
                    ("wire_model", wire_model),
                )
                if not v
            )
        )
        _emit(report, response_text="")
        return 2

    payload = {
        "model": wire_model,
        "max_tokens": MAX_TOKENS,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": token,
        "authorization": f"Bearer {token}",
    }

    # Exactly one real API call. No retry, no fallback, no second attempt.
    body_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{MESSAGES_PATH}", data=body_bytes, headers=headers, method="POST"
    )

    response_text = ""
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            response_text = _extract_text(parsed) if isinstance(parsed, dict) else ""
            report["http_success"] = status == 200
            report["http_status"] = status
            report["response_raw_length"] = len(raw)
            report["response_model"] = parsed.get("model") if isinstance(parsed, dict) else None
            report["http_error"] = None
    except urllib.error.HTTPError as exc:
        report["http_success"] = False
        report["http_status"] = exc.code
        report["http_error"] = _safe_error_body(exc)
    except urllib.error.URLError as exc:
        report["http_success"] = False
        report["http_status"] = None
        report["http_error"] = f"URLError: {exc.reason}"
    except OSError as exc:
        report["http_success"] = False
        report["http_status"] = None
        report["http_error"] = f"OSError: {exc}"

    _emit(report, response_text)
    return 0


def _safe_error_body(exc: urllib.error.HTTPError) -> str:
    """Return a trimmed error body, never including our own auth header."""
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except OSError:
        body = ""
    if not body:
        return f"HTTP {exc.code} ({exc.reason})"
    return f"HTTP {exc.code} ({exc.reason}): {body[:800]}"


def _emit(report: dict, response_text: str) -> None:
    lowered = response_text.lower()
    validation = validate_plan_text(response_text, TASK_TEXT)

    result: dict = {
        **report,
        "response_non_empty": bool(response_text.strip()),
        "response_first_1000_chars": response_text[:1000],
        "contains_greeting_py": "greeting.py" in lowered,
        "contains_test_greeting_py": "test_greeting.py" in lowered,
        "plan_validation": {
            "valid": validation.valid,
            "reason_codes": [c.value for c in validation.reason_codes],
            "matched_task_terms": list(validation.matched_task_terms),
            "length": validation.length,
        },
    }

    # Never print the token / api key / authorization header / full user config.
    print(json.dumps(result, ensure_ascii=False, indent=2))

    out_path = PROJECT_DIR / "runtime" / "diagnose_direct_plan_provider_result.json"
    try:
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n[saved sanitized report -> {out_path}]")
    except OSError:
        print("\n[warn] could not save report file (continuing)")


if __name__ == "__main__":
    raise SystemExit(main())
