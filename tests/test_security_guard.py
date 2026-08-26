"""MemorySecurityGuard seam tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.repository import JsonMemoryRepository
from memory.security_guard import (
    GuardDecision,
    MemorySecurityViolation,
    NoopMemorySecurityGuard,
)
from memory.service import MemoryService


class FakeAdapter:
    def __init__(self) -> None:
        self.add_count = 0

    def add(self, text, metadata=None):
        self.add_count += 1
        return "vec-1"

    def search(self, query, *, limit=5):
        return []

    def delete(self, vector_id):
        return True


class BlockGuard:
    def guard(self, content: str) -> GuardDecision:
        return GuardDecision(action="block", reason="memory poisoning suspected")


class RedactGuard:
    def guard(self, content: str) -> GuardDecision:
        return GuardDecision(action="redact", content=content.replace("咖啡", "茶"))


def _service(tmp_path: Path, guard):
    repo = JsonMemoryRepository(tmp_path / "records.json")
    return MemoryService(repo, FakeAdapter(), security_guard=guard), repo


def test_noop_guard_allows_write(tmp_path: Path) -> None:
    service, repo = _service(tmp_path, NoopMemorySecurityGuard())

    record = service.remember("我喜欢咖啡", asserted_explicit=True)

    assert record is not None
    assert record.content == "我喜欢咖啡"
    assert repo.list() == [record]


def test_block_guard_blocks_write_without_persistence(tmp_path: Path) -> None:
    service, repo = _service(tmp_path, BlockGuard())

    with pytest.raises(MemorySecurityViolation, match="poisoning"):
        service.remember("我喜欢咖啡", asserted_explicit=True)

    assert repo.list() == []


def test_redact_guard_redacts_content(tmp_path: Path) -> None:
    service, repo = _service(tmp_path, RedactGuard())

    record = service.remember("我喜欢咖啡", asserted_explicit=True)

    assert record is not None
    assert record.content == "我喜欢茶"
    assert repo.list() == [record]
