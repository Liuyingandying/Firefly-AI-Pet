"""Scratchpad v1 store — atomic JSON metadata + copied asset files.

Layout (under the store root, default %LOCALAPPDATA%/FireflyAI/scratchpad/):

    items.json          metadata only (text / asset relative path / timestamps)
    assets/<uuid>.<ext> Scratchpad's OWN copy of every dropped image

Rules:
- Images are COPIED in; the user's original file is never read again after
  the copy succeeds and never modified.
- Deletion removes the metadata plus the asset file *only when* it resolves
  inside this store's assets/ directory — a user's original file can never
  be deleted through the Scratchpad.
- No base64 in JSON, ever.
- A corrupt items.json is quarantined (renamed) and the store restarts
  empty instead of crashing.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

from .models import ScratchpadItem, ScratchpadItemType, ScratchpadSource

STORE_VERSION = 1


class ScratchpadStore:
    """Thread-safe, Qt-free persistence for Scratchpad items."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.items_path = self.root / "items.json"
        self.assets_dir = self.root / "assets"
        self._lock = threading.RLock()
        self._items: dict[str, ScratchpadItem] = {}
        self._load()

    # ---------------------------------------------------------------- load

    def _load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        if not self.items_path.exists():
            self._items = {}
            return
        try:
            raw = json.loads(self.items_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Quarantine the corrupt file; never crash the notebook.
            quarantined = self.items_path.with_name(
                f"items.corrupt-{int(time.time())}.json"
            )
            try:
                self.items_path.rename(quarantined)
            except OSError:
                pass
            self._items = {}
            return
        items: dict[str, ScratchpadItem] = {}
        if isinstance(raw, dict):
            entries = raw.get("items")
            if isinstance(entries, list):
                for entry in entries:
                    item = ScratchpadItem.from_dict(entry if isinstance(entry, dict) else {})
                    if item is not None and item.id not in items:
                        items[item.id] = item
        self._items = items

    def _persist(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "items": [item.to_dict() for item in self._items.values()],
        }
        temporary = self.items_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.items_path)

    # ---------------------------------------------------------------- read

    def list_items(self) -> list[ScratchpadItem]:
        """All items, newest first (created_at DESC is the only ordering)."""
        with self._lock:
            return sorted(
                self._items.values(), key=lambda item: item.created_at, reverse=True
            )

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def get(self, item_id: str) -> ScratchpadItem | None:
        with self._lock:
            return self._items.get(item_id)

    def asset_absolute_path(self, item: ScratchpadItem) -> Path | None:
        """Absolute path of an item's asset, or None when absent/unsafe."""
        if item.item_type is not ScratchpadItemType.IMAGE or not item.asset_path:
            return None
        candidate = (self.root / item.asset_path).resolve()
        try:
            candidate.relative_to(self.assets_dir.resolve())
        except ValueError:
            return None  # anything outside assets/ is rejected by construction
        return candidate

    def asset_exists(self, item: ScratchpadItem) -> bool:
        path = self.asset_absolute_path(item)
        return path is not None and path.is_file()

    # --------------------------------------------------------------- write

    def add_text(self, text: str, source: ScratchpadSource | str) -> ScratchpadItem:
        source = ScratchpadSource(source)
        cleaned = str(text).strip()
        if not cleaned:
            raise ValueError("scratchpad text is empty")
        item = ScratchpadItem(
            id=ScratchpadItem.new_id(),
            created_at=ScratchpadItem.now_ms(),
            item_type=ScratchpadItemType.TEXT,
            text=cleaned,
            source=source,
        )
        with self._lock:
            self._items[item.id] = item
            self._persist()
        return item

    def add_image_copy(
        self,
        source_file: Path,
        *,
        safe_extension: str,
        source: ScratchpadSource | str,
        copier=shutil.copy2,
    ) -> ScratchpadItem:
        source = ScratchpadSource(source)
        """Copy an image INTO the store; the original file is never touched.

        ``copier`` is injectable for tests (e.g. simulating disk failure).
        """
        ext = _safe_extension(safe_extension)
        item_id = uuid.uuid4().hex
        relative = f"assets/{item_id}{ext}"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        copier(str(source_file), str(destination))
        item = ScratchpadItem(
            id=ScratchpadItem.new_id(),
            created_at=ScratchpadItem.now_ms(),
            item_type=ScratchpadItemType.IMAGE,
            asset_path=relative,
            source=source,
        )
        with self._lock:
            self._items[item.id] = item
            self._persist()
        return item

    def add_image_bytes(
        self, data: bytes, *, safe_extension: str,
        source: ScratchpadSource | str,
    ) -> ScratchpadItem:
        source = ScratchpadSource(source)
        ext = _safe_extension(safe_extension)
        item_id = uuid.uuid4().hex
        relative = f"assets/{item_id}{ext}"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            destination.write_bytes(data)
            item = ScratchpadItem(
                id=ScratchpadItem.new_id(),
                created_at=ScratchpadItem.now_ms(),
                item_type=ScratchpadItemType.IMAGE,
                asset_path=relative,
                source=source,
            )
            self._items[item.id] = item
            self._persist()
        return item

    def delete(self, item_id: str) -> bool:
        """Remove metadata + the store's own asset copy (never user files)."""
        with self._lock:
            item = self._items.pop(item_id, None)
            if item is None:
                return False
            self._persist()
        if item.item_type is ScratchpadItemType.IMAGE:
            path = self.asset_absolute_path(item)
            if path is not None and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass  # metadata is already gone; orphan cleanup is non-fatal
        return True

    def prune_assets(self, keep: Iterable[str]) -> int:
        """Best-effort removal of orphaned asset files (not part of v1 UI)."""
        keep_set = set(keep)
        removed = 0
        if not self.assets_dir.is_dir():
            return removed
        for path in self.assets_dir.iterdir():
            if path.name in keep_set:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed


_IMAGE_EXTENSIONS = {
    ".png": ".png",
    ".jpg": ".jpg",
    ".jpeg": ".jpg",   # normalise to .jpg for copied assets
    ".webp": ".webp",
    ".bmp": ".bmp",
}


def normalize_image_extension(extension: str) -> str | None:
    """Map a user-supplied extension to a safe asset extension, or None."""
    ext = str(extension or "").lower().strip()
    if not ext.startswith("."):
        ext = f".{ext}"
    return _IMAGE_EXTENSIONS.get(ext)


def _safe_extension(extension: str) -> str:
    normalized = normalize_image_extension(extension)
    if normalized is None:
        raise ValueError(f"unsupported scratchpad image extension: {extension!r}")
    return normalized
