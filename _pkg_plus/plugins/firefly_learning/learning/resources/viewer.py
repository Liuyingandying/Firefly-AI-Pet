"""ResourceViewerAction (Phase 8B-2 Part A).

Turns a :class:`LearningResource` into an OPEN REQUEST — it never opens,
parses, downloads or summarizes anything itself. The UI executes the request
(in production via ``QDesktopServices``, the app's established open-file
mechanism).

    PDF / TEXTBOOK  → open request with the recorded file path (+ page hint)
    everything else → "暂不支持直接打开" (no local viewer exists)

The file path comes from the resource's ``metadata["path"]``, recorded at
textbook-import time. A resource without a recorded path yields an
unsupported request — nothing is guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Resource types the desktop can open locally (with a recorded file path).
_OPENABLE_TYPES = frozenset({"pdf", "textbook"})


@dataclass(frozen=True, slots=True)
class ViewerRequest:
    """The outcome of resolving a resource into an open action."""

    kind: str                 # "pdf" | "unsupported"
    path: str | None = None
    page: int | None = None
    message: str = ""

    @property
    def supported(self) -> bool:
        return self.kind == "pdf" and bool(self.path)


def _first_page(locator: str) -> int | None:
    """"page 35-48" → 35; "page 12" → 12; anything else → None."""
    match = re.search(r"page\s*(\d+)", locator or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


class ResourceViewerAction:
    """Resolves a resource into an open request (pure; no I/O)."""

    def open_request(self, resource: Any | None) -> ViewerRequest:
        """Build the open request for one resource; never raises.

        Unsupported types and unresolvable paths return an ``unsupported``
        request carrying the user-facing message — the caller just shows it.
        """
        if resource is None:
            return ViewerRequest(
                kind="unsupported", message="当前没有可查看的学习材料。"
            )
        resource_type = str(getattr(resource, "resource_type", "") or "").lower()
        title = str(getattr(resource, "title", "") or "") or "该材料"
        metadata = getattr(resource, "metadata", None) or {}
        path = str(metadata.get("path") or "").strip()

        if resource_type not in _OPENABLE_TYPES or not path:
            return ViewerRequest(
                kind="unsupported",
                message=f"暂不支持直接打开「{title}」（{resource_type}）。",
            )

        page = _first_page(str(getattr(resource, "locator", "") or ""))
        page_hint = f"（第 {page} 页）" if page is not None else ""
        return ViewerRequest(
            kind="pdf",
            path=path,
            page=page,
            message=f"已打开「{title}」{page_hint}。",
        )


__all__ = ["ResourceViewerAction", "ViewerRequest"]
