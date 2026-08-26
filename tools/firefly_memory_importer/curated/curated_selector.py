"""Curated selector: pick high-value long-term candidates from a Migration Preview.

Deterministic, no LLM. Prioritizes long-lived, factual content
(``user_fact`` / ``preference`` / ``project`` / ``shared_experience``) and
deprioritizes ``emotion`` and roleplay-tainted content. The selector never
modifies character YAML and never imports the assistant persona — it only
re-ranks the already-extracted candidates.
"""

from __future__ import annotations

from typing import Any

_HIGH_VALUE_MEMORY = frozenset(
    {"user_fact", "preference", "project", "shared_experience"}
)
_HIGH_VALUE_SIGNALS = frozenset(
    {"shared_milestone", "promise_made", "promise_kept", "promise_missed"}
)
_EMOTION_CONFIDENCE_THRESHOLD = 0.6
_PREVIEW_ONLY_KEYS = frozenset({"risk", "preview", "review"})


def select(preview: dict[str, Any]) -> dict[str, Any]:
    """Return ``{selected, skipped}`` for a Migration Preview.

    ``selected`` holds clean (commit-ready) candidate dicts grouped by kind;
    ``skipped`` records what was dropped and why.
    """
    selected: dict[str, list[dict[str, Any]]] = {"memory": [], "bond": [], "style": []}
    skipped: list[dict[str, Any]] = []

    for group in ("memory", "bond", "style"):
        for item in preview.get("groups", {}).get(group, []):
            reason = _skip_reason(item)
            if reason is None:
                selected[group].append(_clean(item))
            else:
                skipped.append(
                    {"id": item.get("id"), "kind": item.get("kind"), "reason": reason}
                )
    return {"selected": selected, "skipped": skipped}


def _skip_reason(item: dict[str, Any]) -> str | None:
    if item.get("band") == "rejected":
        return "rejected-band"
    roleplay = "roleplay" in item.get("flags", [])
    kind = item.get("kind")

    if kind == "memory":
        category = item.get("category")
        if category in _HIGH_VALUE_MEMORY:
            return "roleplay-risk" if roleplay else None
        if category == "emotion":
            if not roleplay and item.get("confidence", 0) >= _EMOTION_CONFIDENCE_THRESHOLD:
                return None
            return "roleplay-risk" if roleplay else "low-value-emotion"
        return f"low-value-category:{category}"

    if kind == "bond":
        signal = item.get("signal_type")
        if signal in _HIGH_VALUE_SIGNALS:
            return "roleplay-risk" if roleplay else None
        return f"mechanical-signal:{signal}"

    if kind == "style":
        return "roleplay-risk" if roleplay else None

    return "unknown-kind"


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in _PREVIEW_ONLY_KEYS}


__all__ = ["select"]
