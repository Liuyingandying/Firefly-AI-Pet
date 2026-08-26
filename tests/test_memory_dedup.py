"""Memory deduplication tests (exact + semantic) and dedup config."""

from __future__ import annotations

from pathlib import Path

from core.companion_config import CompanionConfig, load_companion_config
from memory.mem0_adapter import Hit
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService, WriteOutcome


class FakeAdapter:
    def __init__(self, *, search_score: float = 0.95, fail_search: bool = False):
        self.entries: dict[str, dict] = {}
        self.add_calls = 0
        self.search_score = search_score
        self.fail_search = fail_search

    def add(self, text, metadata=None):
        self.add_calls += 1
        vid = f"vec-{len(self.entries) + 1}"
        self.entries[vid] = {"text": text, "metadata": dict(metadata or {})}
        return vid

    def search(self, query, *, limit=5, threshold=0.0):
        if self.fail_search:
            raise RuntimeError("mem0 unavailable")
        return [
            Hit(vid, e["text"], dict(e["metadata"]), self.search_score)
            for vid, e in self.entries.items()
        ][:limit]

    def delete(self, vector_id):
        return self.entries.pop(vector_id, None) is not None


def _service(tmp_path: Path, adapter, **kwargs) -> MemoryService:
    return MemoryService(JsonMemoryRepository(tmp_path / "records.json"), adapter, **kwargs)


# --- exact dedup -----------------------------------------------------------


def test_exact_duplicate_identical_suppressed(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    service = _service(tmp_path, adapter)

    r1 = service.remember_detailed("我喜欢极简 UI", asserted_explicit=True)
    r2 = service.remember_detailed("  我喜欢极简 UI。 ", asserted_explicit=True)

    assert r1.outcome is WriteOutcome.CREATED
    assert r2.outcome is WriteOutcome.EXACT_DUPLICATE
    assert r2.record.id == r1.record.id
    assert len(service.repository.list()) == 1
    assert adapter.add_calls == 1


def test_exact_duplicate_preserves_original_content(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    service = _service(tmp_path, adapter)

    service.remember_detailed("我喜欢极简 UI", asserted_explicit=True)
    service.remember_detailed("  我喜欢极简 UI。 ", asserted_explicit=True)

    stored = service.repository.list()[0]
    assert stored.content == "我喜欢极简 UI"


# --- semantic dedup --------------------------------------------------------


def test_semantic_duplicate_same_category_suppressed(tmp_path: Path) -> None:
    adapter = FakeAdapter(search_score=0.95)
    service = _service(tmp_path, adapter)

    r1 = service.remember_detailed(
        "用户喜欢极简风格的界面。", category="preference", asserted_explicit=True
    )
    r2 = service.remember_detailed(
        "用户偏好简洁、极简的 UI。", category="preference", asserted_explicit=True
    )

    assert r1.outcome is WriteOutcome.CREATED
    assert r2.outcome is WriteOutcome.SEMANTIC_DUPLICATE
    assert r2.record.id == r1.record.id
    assert len(service.repository.list()) == 1
    assert adapter.add_calls == 1


def test_semantic_dedup_different_category_not_suppressed(tmp_path: Path) -> None:
    adapter = FakeAdapter(search_score=0.95)
    service = _service(tmp_path, adapter)

    r1 = service.remember_detailed(
        "用户喜欢极简风格的界面。", category="preference", asserted_explicit=True
    )
    r2 = service.remember_detailed(
        "用户偏好简洁、极简的 UI。", category="project", asserted_explicit=True
    )

    assert r1.outcome is WriteOutcome.CREATED
    assert r2.outcome is WriteOutcome.CREATED
    assert len(service.repository.list()) == 2


def test_semantic_dedup_below_threshold_creates(tmp_path: Path) -> None:
    adapter = FakeAdapter(search_score=0.5)
    service = _service(tmp_path, adapter)

    r1 = service.remember_detailed(
        "用户喜欢极简风格的界面。", category="preference", asserted_explicit=True
    )
    r2 = service.remember_detailed(
        "用户偏好简洁、极简的 UI。", category="preference", asserted_explicit=True
    )

    assert r1.outcome is WriteOutcome.CREATED
    assert r2.outcome is WriteOutcome.CREATED
    assert len(service.repository.list()) == 2


def test_semantic_dedup_mem0_unavailable_degrades(tmp_path: Path) -> None:
    adapter = FakeAdapter(fail_search=True)
    service = _service(tmp_path, adapter)

    r1 = service.remember_detailed(
        "用户喜欢极简风格的界面。", category="preference", asserted_explicit=True
    )
    r2 = service.remember_detailed(
        "用户偏好简洁、极简的 UI。", category="preference", asserted_explicit=True
    )

    assert r1.outcome is WriteOutcome.CREATED
    assert r2.outcome is WriteOutcome.CREATED
    assert len(service.repository.list()) == 2


def test_dedup_disabled_skips_semantic(tmp_path: Path) -> None:
    adapter = FakeAdapter(search_score=0.95)
    service = _service(tmp_path, adapter, dedup_enabled=False)

    r1 = service.remember_detailed(
        "用户喜欢极简风格的界面。", category="preference", asserted_explicit=True
    )
    r2 = service.remember_detailed(
        "用户偏好简洁、极简的 UI。", category="preference", asserted_explicit=True
    )

    assert r1.outcome is WriteOutcome.CREATED
    assert r2.outcome is WriteOutcome.CREATED
    assert len(service.repository.list()) == 2


# --- config ----------------------------------------------------------------


def test_config_dedup_defaults() -> None:
    cfg = CompanionConfig()
    assert cfg.memory.dedup_enabled is True
    assert cfg.memory.dedup_similarity_threshold == 0.85


def test_config_invalid_threshold_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "companion.json"
    path.write_text(
        '{"memory": {"dedup_similarity_threshold": 2.0}}', encoding="utf-8"
    )
    cfg = load_companion_config(path)
    assert cfg.memory.dedup_similarity_threshold == 0.85
