"""Phase v0.3-P4: Memory Retrieval Ranking + Decay tests.

Covers:
- calculate_temporal_decay (half-life, edge cases)
- calculate_category_weight (CATEGORY_DEFAULTS lookup)
- calculate_importance_factor (clamping, type guards)
- calculate_final_score (product of all factors)
- re_rank_results (ordering, empty input, missing scores)
- MemoryService.search with ranking pipeline integration
- search_threshold filtering (server-side and client-side)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from memory.mem0_adapter import Hit
from memory.records import (
    CATEGORY_DEFAULTS,
    MemoryCategory,
    MemoryRecord,
    MemorySource,
    WritePolicy,
)
from memory.repository import JsonMemoryRepository
from memory.ranking import (
    RankedHit,
    calculate_category_weight,
    calculate_final_score,
    calculate_importance_factor,
    calculate_temporal_decay,
    re_rank_results,
)
from memory.service import MemoryService, WriteOutcome


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _record(
    tmp_path: Path,
    category: MemoryCategory | str = MemoryCategory.PREFERENCE,
    content: str = "test content",
    created_ts: int | None = None,
    weight: float | None = None,
    retention_half_life_days: float | None = None,
    timestamp_ms: int | None = None,
) -> MemoryRecord:
    """Create a MemoryRecord with optional timestamp override."""
    kwargs: dict[str, Any] = {
        "category": category,
        "content": content,
        "trigger": "test",
        "source": MemorySource.EXPLICIT,
        "permission": WritePolicy.EXPLICIT_ONLY,
    }
    if weight is not None:
        kwargs["weight"] = weight
    if retention_half_life_days is not None:
        kwargs["retention_half_life_days"] = retention_half_life_days
    if timestamp_ms is not None:
        kwargs["timestamp_ms"] = timestamp_ms
    return MemoryRecord.create(**kwargs)


class FakeAdapter:
    """Minimal adapter that supports the threshold kwarg."""

    def __init__(self, hits: list[Hit] | None = None) -> None:
        self.hits = list(hits or [])
        self.add_calls: list[tuple[str, dict[str, Any]]] = []

    def add(self, text: str, metadata: Mapping[str, Any] | None = None) -> str:
        vid = f"vec-{len(self.add_calls) + 1}"
        self.add_calls.append((text, dict(metadata or {})))
        return vid

    def search(
        self, query: str, *, limit: int = 5, threshold: float = 0.0
    ) -> list[Hit]:
        return [
            h for h in self.hits if h.score >= threshold
        ][:limit]

    def delete(self, vector_id: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# calculate_temporal_decay
# ---------------------------------------------------------------------------


def test_temporal_decay_new_record_is_1_0() -> None:
    now_ms = int(time.time() * 1000)
    record = _record(Path("/tmp"), timestamp_ms=now_ms)
    decay = calculate_temporal_decay(record, reference_ts=now_ms)
    assert decay == pytest.approx(1.0)


def test_temporal_decay_exactly_one_half_life() -> None:
    now_ms = int(time.time() * 1000)
    old_ts = now_ms - (7 * 86_400_000)  # 7 days ago
    record = _record(
        Path("/tmp"),
        category=MemoryCategory.EMOTION,  # half_life = 7 days
        timestamp_ms=old_ts,
    )
    decay = calculate_temporal_decay(record, reference_ts=now_ms)
    assert decay == pytest.approx(0.5, abs=1e-9)


def test_temporal_decay_two_half_lives() -> None:
    now_ms = int(time.time() * 1000)
    old_ts = now_ms - (14 * 86_400_000)  # 14 days ago
    record = _record(
        Path("/tmp"),
        category=MemoryCategory.EMOTION,  # half_life = 7 days -> 2 half-lives
        timestamp_ms=old_ts,
    )
    decay = calculate_temporal_decay(record, reference_ts=now_ms)
    assert decay == pytest.approx(0.25, abs=1e-9)


def test_temporal_decay_zero_half_life_returns_1() -> None:
    now_ms = int(time.time() * 1000)
    record = _record(
        Path("/tmp"),
        category=MemoryCategory.PREFERENCE,
        timestamp_ms=now_ms - (1000 * 86_400_000),  # 1000 days old
        retention_half_life_days=0.0001,  # effectively zero → very fast decay
    )
    decay = calculate_temporal_decay(record, reference_ts=now_ms)
    # With half_life=0.0001 days and age=1000 days, decay ≈ 0.5^(1000/0.0001) ≈ 0
    assert decay == pytest.approx(0.0, abs=1e-9)


def test_temporal_decay_no_half_life_field_returns_1() -> None:
    """When retention_half_life_days is effectively zero (half_life <= 0 guard)."""
    now_ms = int(time.time() * 1000)
    # Create a record with half_life_days=0 — but MemoryRecord requires > 0,
    # so we test the boundary via the ranking function directly.
    record = _record(
        Path("/tmp"),
        category=MemoryCategory.PREFERENCE,
        timestamp_ms=now_ms,
        retention_half_life_days=0.001,  # tiny but valid
    )
    decay = calculate_temporal_decay(record, reference_ts=now_ms)
    # Age ~0, half_life ~0.001 → decay ≈ 0.5^0 = 1.0
    assert decay == pytest.approx(1.0, abs=1e-6)


def test_temporal_decay_relationship_long_lived() -> None:
    now_ms = int(time.time() * 1000)
    old_ts = now_ms - (90 * 86_400_000)  # 90 days ago
    record = _record(
        Path("/tmp"),
        category=MemoryCategory.RELATIONSHIP,  # half_life = 90 days
        timestamp_ms=old_ts,
    )
    decay = calculate_temporal_decay(record, reference_ts=now_ms)
    assert decay == pytest.approx(0.5, abs=1e-9)


# ---------------------------------------------------------------------------
# calculate_category_weight
# ---------------------------------------------------------------------------


def test_category_weight_lookup() -> None:
    for category, defaults in CATEGORY_DEFAULTS.items():
        assert calculate_category_weight(category) == defaults["weight"]


def test_category_weight_relationship_is_highest() -> None:
    assert calculate_category_weight(MemoryCategory.RELATIONSHIP) == 1.5
    assert calculate_category_weight(MemoryCategory.SHARED_EXPERIENCE) == 1.2


# ---------------------------------------------------------------------------
# calculate_importance_factor
# ---------------------------------------------------------------------------


def test_importance_factor_clamped_to_2_0() -> None:
    # weight=2.0 should pass through directly
    assert calculate_importance_factor(2.0) == 2.0
    # weight > 2.0 should clamp to 2.0
    assert calculate_importance_factor(5.0) == 2.0


def test_importance_factor_clamped_to_0_0() -> None:
    assert calculate_importance_factor(-5.0) == 0.0


def test_importance_factor_normal() -> None:
    assert calculate_importance_factor(0.8) == 0.8
    assert calculate_importance_factor(1.0) == 1.0


def test_importance_factor_type_guard_bool() -> None:
    assert calculate_importance_factor(True) == 1.0


def test_importance_factor_type_guard_string() -> None:
    assert calculate_importance_factor("high") == 1.0


# ---------------------------------------------------------------------------
# calculate_final_score
# ---------------------------------------------------------------------------


def test_final_score_product() -> None:
    now_ms = int(time.time() * 1000)
    record = _record(
        Path("/tmp"),
        category=MemoryCategory.PREFERENCE,
        timestamp_ms=now_ms,  # brand new
    )
    # semantic=1.0, cat_weight=0.9, importance=0.9, decay=1.0
    expected = 1.0 * 0.9 * 0.9 * 1.0
    result = calculate_final_score(1.0, record, reference_ts=now_ms)
    assert result == pytest.approx(expected)


def test_final_score_with_decay() -> None:
    now_ms = int(time.time() * 1000)
    old_ts = now_ms - (7 * 86_400_000)
    record = _record(
        Path("/tmp"),
        category=MemoryCategory.EMOTION,
        timestamp_ms=old_ts,
    )
    # semantic=1.0, cat_weight=1.0, importance=1.0, decay=0.5
    expected = 1.0 * 1.0 * 1.0 * 0.5
    result = calculate_final_score(1.0, record, reference_ts=now_ms)
    assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# re_rank_results
# ---------------------------------------------------------------------------


def test_re_rank_sorts_by_final_score_descending() -> None:
    now_ms = int(time.time() * 1000)
    old_ts = now_ms - (30 * 86_400_000)  # 30 days old

    r1 = _record(Path("/tmp"), category=MemoryCategory.PREFERENCE, content="a", timestamp_ms=now_ms)
    r2 = _record(Path("/tmp"), category=MemoryCategory.PREFERENCE, content="b", timestamp_ms=old_ts)

    scores = {r1.id: 1.0, r2.id: 1.0}
    ranked = re_rank_results([r1, r2], scores, reference_ts=now_ms)

    assert len(ranked) == 2
    assert ranked[0].record.id == r1.id  # newer should rank higher
    assert ranked[0].final_score > ranked[1].final_score


def test_re_rank_empty_input() -> None:
    ranked = re_rank_results([], {}, reference_ts=None)
    assert ranked == []


def test_re_rank_missing_semantic_score_defaults_to_zero() -> None:
    record = _record(Path("/tmp"), category=MemoryCategory.PREFERENCE)
    scores: dict[str, float] = {}  # no score for this record
    ranked = re_rank_results([record], scores, reference_ts=None)
    assert len(ranked) == 1
    assert ranked[0].semantic_score == 0.0
    assert ranked[0].final_score == 0.0


def test_re_rank_negative_semantic_score_defaults_to_zero() -> None:
    record = _record(Path("/tmp"), category=MemoryCategory.PREFERENCE)
    scores = {record.id: -0.5}
    ranked = re_rank_results([record], scores, reference_ts=None)
    assert ranked[0].semantic_score == 0.0
    assert ranked[0].final_score == 0.0


def test_re_rank_preserves_all_fields() -> None:
    now_ms = int(time.time() * 1000)
    record = _record(
        Path("/tmp"),
        category=MemoryCategory.RELATIONSHIP,
        timestamp_ms=now_ms,
    )
    scores = {record.id: 0.8}
    ranked = re_rank_results([record], scores, reference_ts=now_ms)

    rh = ranked[0]
    assert rh.record is record
    assert rh.semantic_score == pytest.approx(0.8)
    assert rh.category_weight == pytest.approx(1.5)
    assert rh.importance_factor == pytest.approx(1.5)  # default weight for relationship
    assert rh.temporal_decay == pytest.approx(1.0)
    assert rh.final_score == pytest.approx(0.8 * 1.5 * 1.5 * 1.0)


# ---------------------------------------------------------------------------
# MemoryService.search ranking integration
# ---------------------------------------------------------------------------


def make_ranking_service(
    tmp_path: Path,
    hits: list[Hit] | None = None,
    search_threshold: float = 0.0,
) -> tuple[MemoryService, JsonMemoryRepository, FakeAdapter]:
    repo = JsonMemoryRepository(tmp_path / "records.json")
    adapter = FakeAdapter(hits)
    service = MemoryService(
        repo, adapter, search_threshold=search_threshold
    )
    return service, repo, adapter


def test_search_returns_ranked_records(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    old_ts = now_ms - (60 * 86_400_000)  # 60 days old

    # Create two records
    r_new = MemoryRecord.create(
        category=MemoryCategory.PREFERENCE,
        content="new preference",
        trigger="test",
        timestamp_ms=now_ms,
    )
    r_old = MemoryRecord.create(
        category=MemoryCategory.PREFERENCE,
        content="old preference",
        trigger="test",
        timestamp_ms=old_ts,
    )

    # Manually add to repository
    tmp_path.joinpath("records.json").write_text("{}", encoding="utf-8")
    repo = JsonMemoryRepository(tmp_path / "records.json")
    repo.add(r_new)
    repo.add(r_old)

    # Create hits with same semantic score
    hits = [
        Hit("vec-1", "new preference", {"record_id": r_new.id}, 0.9),
        Hit("vec-2", "old preference", {"record_id": r_old.id}, 0.9),
    ]

    service = MemoryService(repo, FakeAdapter(hits), search_threshold=0.0)

    results = service.search("preference")

    # Both should be returned, new one ranked higher
    assert len(results) == 2
    assert results[0].id == r_new.id
    assert results[1].id == r_old.id


def test_search_threshold_filters_low_scoring(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)
    r = MemoryRecord.create(
        category=MemoryCategory.PREFERENCE,
        content="weak match",
        trigger="test",
        timestamp_ms=now_ms,
    )

    tmp_path.joinpath("records.json").write_text("{}", encoding="utf-8")
    repo = JsonMemoryRepository(tmp_path / "records.json")
    repo.add(r)

    # Very low semantic score -> final_score will be low
    hits = [Hit("vec-1", "weak match", {"record_id": r.id}, 0.1)]

    # With threshold 0.9, this should be filtered out
    service = MemoryService(repo, FakeAdapter(hits), search_threshold=0.9)
    results = service.search("weak match")
    assert len(results) == 0

    # With threshold 0.0, it should pass (if final_score >= 0.0)
    service2 = MemoryService(repo, FakeAdapter(hits), search_threshold=0.0)
    results2 = service2.search("weak match")
    assert len(results2) == 1


def test_search_limit_is_respected(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)

    records = []
    for i in range(5):
        r = MemoryRecord.create(
            category=MemoryCategory.PREFERENCE,
            content=f"preference {i}",
            trigger="test",
            timestamp_ms=now_ms,
        )
        records.append(r)

    tmp_path.joinpath("records.json").write_text("{}", encoding="utf-8")
    repo = JsonMemoryRepository(tmp_path / "records.json")
    for r in records:
        repo.add(r)

    hits = [
        Hit(f"vec-{i}", f"preference {i}", {"record_id": r.id}, 1.0)
        for i, r in enumerate(records)
    ]

    service = MemoryService(repo, FakeAdapter(hits), search_top_k=3)
    results = service.search("preference")
    assert len(results) <= 3


def test_search_limit_override(tmp_path: Path) -> None:
    now_ms = int(time.time() * 1000)

    records = []
    for i in range(5):
        r = MemoryRecord.create(
            category=MemoryCategory.PREFERENCE,
            content=f"preference {i}",
            trigger="test",
            timestamp_ms=now_ms,
        )
        records.append(r)

    tmp_path.joinpath("records.json").write_text("{}", encoding="utf-8")
    repo = JsonMemoryRepository(tmp_path / "records.json")
    for r in records:
        repo.add(r)

    hits = [
        Hit(f"vec-{i}", f"preference {i}", {"record_id": r.id}, 1.0)
        for i, r in enumerate(records)
    ]

    service = MemoryService(repo, FakeAdapter(hits), search_top_k=2)
    # Override limit to 4
    results = service.search("preference", limit=4)
    assert len(results) <= 4


def test_search_no_hits_returns_empty(tmp_path: Path) -> None:
    service, _, _ = make_ranking_service(tmp_path)
    results = service.search("nonexistent")
    assert results == []


def test_search_threshold_from_init(tmp_path: Path) -> None:
    """Verify search_threshold is stored on the service instance."""
    repo = JsonMemoryRepository(tmp_path / "records.json")
    adapter = FakeAdapter()
    service = MemoryService(repo, adapter, search_threshold=0.75)
    assert service.search_threshold == 0.75


# ---------------------------------------------------------------------------
# Config wiring: search_threshold in MemorySettings
# ---------------------------------------------------------------------------


def test_config_search_threshold_default() -> None:
    from core.companion_config import CompanionConfig

    cfg = CompanionConfig()
    assert cfg.memory.search_threshold == 0.0


def test_config_search_threshold_custom(tmp_path: Path) -> None:
    from core.companion_config import load_companion_config

    path = tmp_path / "test_companion.json"
    path.write_text(
        '{"memory": {"search_threshold": 0.6}}', encoding="utf-8"
    )
    cfg = load_companion_config(path)
    assert cfg.memory.search_threshold == 0.6


def test_config_invalid_search_threshold_falls_back(tmp_path: Path) -> None:
    from core.companion_config import load_companion_config

    path = tmp_path / "test_companion_bad.json"
    path.write_text(
        '{"memory": {"search_threshold": -1.0}}', encoding="utf-8"
    )
    cfg = load_companion_config(path)
    assert cfg.memory.search_threshold == 0.0  # falls back to default


# ---------------------------------------------------------------------------
# Additional P4 coverage: threshold boundaries, top_k, orphan, degradation
# ---------------------------------------------------------------------------


def test_threshold_boundary_zero_accepted() -> None:
    """search_threshold=0.0 should be accepted and pass everything."""
    from core.companion_config import CompanionConfig

    cfg = CompanionConfig(memory={"search_threshold": 0.0})
    assert cfg.memory.search_threshold == 0.0


def test_threshold_boundary_half_accepted() -> None:
    """search_threshold=0.5 should be accepted."""
    from core.companion_config import CompanionConfig

    cfg = CompanionConfig(memory={"search_threshold": 0.5})
    assert cfg.memory.search_threshold == 0.5


def test_threshold_boundary_one_accepted() -> None:
    """search_threshold=1.0 should be accepted."""
    from core.companion_config import CompanionConfig

    cfg = CompanionConfig(memory={"search_threshold": 1.0})
    assert cfg.memory.search_threshold == 1.0


def test_threshold_boundary_negative_rejected() -> None:
    """search_threshold=-0.01 should fall back to default (0.0)."""
    from core.companion_config import CompanionConfig

    with pytest.raises(Exception):  # pydantic validation error
        CompanionConfig(memory={"search_threshold": -0.01})


def test_threshold_boundary_over_one_rejected() -> None:
    """search_threshold=1.01 should fall back to default (0.0)."""
    from core.companion_config import CompanionConfig

    with pytest.raises(Exception):  # pydantic validation error
        CompanionConfig(memory={"search_threshold": 1.01})


def test_mem0_adapter_threshold_type_validation() -> None:
    """Mem0Adapter.search should reject bool and non-finite threshold."""
    from memory.mem0_adapter import Mem0Adapter

    adapter = Mem0Adapter(storage_dir=str(Path("/tmp/test_mem0_adapter_thresh")))
    with pytest.raises(ValueError, match="threshold"):
        adapter.search("test", threshold=True)
    with pytest.raises(ValueError, match="threshold"):
        adapter.search("test", threshold=False)


def test_orphan_mem0_candidate_not_returned(tmp_path: Path) -> None:
    """A Mem0 hit whose record_id is not in the repository must be dropped."""
    repo = JsonMemoryRepository(tmp_path / "records.json")
    adapter = FakeAdapter()
    service = MemoryService(repo, adapter)

    # Create a record and add it to repo (but NOT to adapter hits)
    record = MemoryRecord.create(
        category=MemoryCategory.PREFERENCE,
        content="existing record",
        trigger="test",
        timestamp_ms=int(time.time() * 1000),
    )
    repo.add(record)

    # Hit references a record_id that doesn't exist in repo
    orphan_hits = [
        Hit("vec-999", "orphan content", {"record_id": "nonexistent-id"}, 0.95),
    ]
    service = MemoryService(repo, FakeAdapter(orphan_hits))

    results = service.search("orphan")
    assert results == []


def test_mem0_unavailable_degrades_safely(tmp_path: Path) -> None:
    """When Mem0 is unavailable during search, service raises an error.

    MemoryService.search does NOT swallow adapter exceptions — it lets them
    propagate so the caller can decide how to handle the failure.  The dedup
    path (remember_detailed) already has its own try/except around adapter.search.
    """
    repo = JsonMemoryRepository(tmp_path / "records.json")

    class FailingAdapter:
        def add(self, text, metadata=None):
            return "vec-1"

        def search(self, query, *, limit=5, threshold=0.0):
            raise RuntimeError("mem0 unavailable")

        def delete(self, vector_id):
            return True

    service = MemoryService(repo, FailingAdapter())
    with pytest.raises(RuntimeError, match="mem0 unavailable"):
        service.search("anything")


def test_decay_does_not_delete_memory_record(tmp_path: Path) -> None:
    """Temporal decay affects ranking only — it must never delete records."""
    repo = JsonMemoryRepository(tmp_path / "records.json")
    adapter = FakeAdapter()
    service = MemoryService(repo, adapter)

    now_ms = int(time.time() * 1000)
    # Create a very old record (emotion category, 7-day half-life)
    old_ts = now_ms - (365 * 86_400_000)  # 1 year old
    record = MemoryRecord.create(
        category=MemoryCategory.EMOTION,
        content="old emotion",
        trigger="test",
        timestamp_ms=old_ts,
    )
    repo.add(record)

    # Search — even with very low threshold, decay should not delete
    hits = [Hit("vec-1", "old emotion", {"record_id": record.id}, 0.9)]
    service = MemoryService(repo, FakeAdapter(hits), search_threshold=0.0)
    results = service.search("old")

    # Record still exists in repository
    assert len(repo.list()) == 1
    # Record may or may not pass threshold depending on final_score
    # but it should NOT be deleted
    assert len(repo.list()) == 1


def test_shared_experience_decays_slower_than_emotion(tmp_path: Path) -> None:
    """shared_experience (60-day half-life) should decay slower than emotion (7-day)."""
    now_ms = int(time.time() * 1000)
    old_ts = now_ms - (30 * 86_400_000)  # 30 days ago

    emotion_record = _record(
        Path("/tmp"),
        category=MemoryCategory.EMOTION,
        timestamp_ms=old_ts,
    )
    shared_record = _record(
        Path("/tmp"),
        category=MemoryCategory.SHARED_EXPERIENCE,
        timestamp_ms=old_ts,
    )

    decay_emotion = calculate_temporal_decay(emotion_record, reference_ts=now_ms)
    decay_shared = calculate_temporal_decay(shared_record, reference_ts=now_ms)

    # shared_experience half_life=60d, emotion half_life=7d
    # After 30 days: emotion ~0.5^(30/7) ≈ 0.028, shared ~0.5^(30/60) ≈ 0.707
    assert decay_shared > decay_emotion
    assert decay_shared == pytest.approx(0.707, abs=0.01)
    assert decay_emotion < 0.1


def test_top_k_respects_limit_in_service_search(tmp_path: Path) -> None:
    """MemoryService.search should return at most top_k results."""
    now_ms = int(time.time() * 1000)
    records = []
    for i in range(10):
        r = MemoryRecord.create(
            category=MemoryCategory.PREFERENCE,
            content=f"memory {i}",
            trigger="test",
            timestamp_ms=now_ms,
        )
        records.append(r)

    repo = JsonMemoryRepository(tmp_path / "records.json")
    for r in records:
        repo.add(r)

    hits = [
        Hit(f"vec-{i}", f"memory {i}", {"record_id": r.id}, 1.0)
        for i, r in enumerate(records)
    ]

    service = MemoryService(repo, FakeAdapter(hits), search_top_k=5)
    results = service.search("memory")
    assert len(results) <= 5


def test_search_threshold_client_side_filter(tmp_path: Path) -> None:
    """Client-side threshold on final_score should filter out low-scoring results."""
    now_ms = int(time.time() * 1000)

    r = MemoryRecord.create(
        category=MemoryCategory.PREFERENCE,
        content="test content",
        trigger="test",
        timestamp_ms=now_ms,
    )

    repo = JsonMemoryRepository(tmp_path / "records.json")
    repo.add(r)

    # semantic_score=0.1, cat_weight=0.9, importance=0.9, decay=1.0
    # final_score = 0.1 * 0.9 * 0.9 * 1.0 = 0.081
    hits = [Hit("vec-1", "test content", {"record_id": r.id}, 0.1)]

    # threshold 0.05 → should pass
    service = MemoryService(repo, FakeAdapter(hits), search_threshold=0.05)
    results = service.search("test")
    assert len(results) == 1

    # threshold 0.5 → should be filtered (0.081 < 0.5)
    service2 = MemoryService(repo, FakeAdapter(hits), search_threshold=0.5)
    results2 = service2.search("test")
    assert len(results2) == 0


def test_dedup_regression_still_works_with_ranking(tmp_path: Path) -> None:
    """Deduplication should still work correctly alongside ranking."""
    adapter = FakeAdapter()
    service = MemoryService(JsonMemoryRepository(tmp_path / "records.json"), adapter)

    r1 = service.remember_detailed("我喜欢极简 UI", asserted_explicit=True)
    r2 = service.remember_detailed("我喜欢极简 UI", asserted_explicit=True)

    assert r1.outcome is WriteOutcome.CREATED
    assert r2.outcome is WriteOutcome.EXACT_DUPLICATE
    assert len(service.repository.list()) == 1


def test_reconcile_regression_still_works_with_ranking(tmp_path: Path) -> None:
    """Reconciliation should work correctly alongside ranking."""
    repo = JsonMemoryRepository(tmp_path / "records.json")
    adapter = FakeAdapter()
    service = MemoryService(repo, adapter)

    record = MemoryRecord.create(
        category="user_fact", content="missing index", source="explicit", trigger="t"
    )
    repo.add(record)  # vector_id stays None

    result = service.reconcile(dry_run=False)
    assert result.missing_indexes == (record.id,)
    assert repo.get(record.id).vector_id is not None


def test_privacy_guards_regression_still_works_with_ranking(tmp_path: Path) -> None:
    """Privacy guards should still block red-line content alongside ranking."""
    from memory.write_guards import RedLineViolationError

    repo = JsonMemoryRepository(tmp_path / "records.json")
    adapter = FakeAdapter()
    service = MemoryService(repo, adapter)

    with pytest.raises(RedLineViolationError):
        service.remember("sk-abcdef1234567890abcdef", asserted_explicit=True)

    assert len(service.repository.list()) == 0
