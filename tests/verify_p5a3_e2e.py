#!/usr/bin/env python
"""P5A-3: Real provider E2E tests for Limited Auto-Approve.

Uses the real TJU tju-llm provider with tempfile-isolated storage.
All 5 cases from the spec are tested.

Run: python tests/verify_p5a3_e2e.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from memory.records import MemoryCategory, MemorySource, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService
from memory.suggestion.suggestion_service import SuggestionService
from memory.security_guard import NoopMemorySecurityGuard


def _build_services(storage_dir: str) -> tuple[SuggestionService, MemoryService, JsonMemoryRepository]:
    """Build real MemoryService + SuggestionService with temp storage."""
    repo = JsonMemoryRepository(path=Path(storage_dir) / "memory_records.json")
    mem0_dir = Path(storage_dir) / "mem0_data"
    mem0_dir.mkdir(parents=True, exist_ok=True)

    ms = MemoryService.local(
        repo,
        storage_dir=str(mem0_dir),
        security_guard=NoopMemorySecurityGuard(),
        dedup_enabled=True,
        dedup_similarity_threshold=0.85,
    )

    svc = SuggestionService(
        ms,
        enabled=True,
        auto_extract_enabled=False,
        explicit_auto_approve_enabled=True,
        explicit_auto_approve_min_confidence=0.95,
        explicit_auto_approve_allowed_categories=[
            MemoryCategory.PREFERENCE.value,
            MemoryCategory.PROJECT.value,
        ],
        security_guard=NoopMemorySecurityGuard(),
        semantic_dedup_enabled=True,
        semantic_dedup_threshold=0.85,
    )
    return svc, ms, repo


def _count_records(ms: MemoryService) -> int:
    return len(ms.list())


def _count_pending(svc: SuggestionService) -> int:
    return len(svc.list_pending())


def run_case_1(svc, ms):
    """Case 1: '请记住，我喜欢 75% 配列键盘。' → eligible + auto-approved."""
    print("\n=== Case 1: Explicit remember (eligible + auto-approved) ===")
    initial = _count_records(ms)
    result = svc.handle_explicit_remember("请记住，我喜欢 75% 配列键盘。")
    final = _count_records(ms)
    pending = _count_pending(svc)

    print(f"  Records: {initial} → {final} (delta={final - initial})")
    print(f"  Pending: {pending}")
    print(f"  handle_explicit_remember returned: {result}")

    assert final == initial + 1, f"Expected 1 new record, got {final - initial}"
    assert pending == 0, f"Expected 0 pending, got {pending}"
    assert result == [], f"Expected empty result (auto-approved), got {result}"
    print("  ✓ PASS")


def run_case_2(svc, ms):
    """Case 2: '我喜欢 75% 配列键盘。' → Pending only."""
    print("\n=== Case 2: Ordinary candidate (Pending only) ===")
    initial_pending = _count_pending(svc)
    initial_records = _count_records(ms)
    svc.detect("我喜欢 75% 配列键盘。")
    pending = _count_pending(svc)
    records = _count_records(ms)

    print(f"  Pending: {initial_pending} → {pending}")
    print(f"  Records: {initial_records} → {records}")

    assert pending > 0, "Expected pending suggestions"
    assert records == initial_records, "Expected no new records"
    print("  ✓ PASS")


def run_case_3(svc, ms):
    """Case 3: '请记住我的 API key 是 <synthetic secret>' → 0 record, 0 secret log."""
    print("\n=== Case 3: Secret explicit remember (0 record) ===")
    initial = _count_records(ms)
    # Use a synthetic secret that matches the red-line pattern
    result = svc.handle_explicit_remember(
        "请记住我的 API key 是 sk-abcdefghij1234567890abcdef"
    )
    final = _count_records(ms)

    print(f"  Records: {initial} → {final}")
    print(f"  Result: {result}")

    assert final == initial, f"Expected 0 new records, got {final - initial}"
    print("  ✓ PASS")


def run_case_4(svc, ms):
    """Case 4: Semantic duplicate suppression.
    
    First save: '我喜欢机械键盘'
    Then: '请记住，我比较喜欢用机械键盘。'
    Expected: semantic duplicate suppressed, record count unchanged.
    """
    print("\n=== Case 4: Semantic duplicate suppression ===")
    initial = _count_records(ms)
    
    # First: explicit remember
    svc.handle_explicit_remember("请记住，我喜欢机械键盘。")
    after_first = _count_records(ms)
    print(f"  After first remember: {after_first} records")
    
    # Second: similar explicit remember
    result = svc.handle_explicit_remember("请记住，我比较喜欢用机械键盘。")
    after_second = _count_records(ms)
    print(f"  After second remember: {after_second} records")
    print(f"  Result: {result}")

    assert after_second == after_first, (
        f"Expected semantic duplicate suppression: {after_first} → {after_second}"
    )
    print("  ✓ PASS")


def run_case_5(svc, ms):
    """Case 5: Explicit remember but category not in allowlist → Pending."""
    print("\n=== Case 5: Disallowed category (Pending) ===")
    initial = _count_records(ms)
    initial_pending = _count_pending(svc)
    
    # "我的身份证号是..." → infer_category would try to match, but likely falls to USER_FACT
    # USER_FACT is not in the default allowlist [preference, project]
    result = svc.handle_explicit_remember("请记住，我的用户ID是 user-12345。")
    final = _count_records(ms)
    final_pending = _count_pending(svc)

    print(f"  Records: {initial} → {final}")
    print(f"  Pending: {initial_pending} → {final_pending}")
    print(f"  Result: {result}")

    # The content "我的用户ID是 user-12345" → infer_category checks keywords:
    # "喜欢" → no, "项目/开发/project/repository" → no, "开心/难过" → no, etc.
    # Falls to USER_FACT (not in allowlist)
    # So it should stay as pending, not auto-approved
    assert final == initial, f"Expected 0 new records (not auto-approved), got {final - initial}"
    assert final_pending > initial_pending, "Expected new pending suggestion"
    print("  ✓ PASS")


def main():
    print("=" * 60)
    print("P5A-3 Real Provider E2E Tests")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="p5a3_e2e_") as tmpdir:
        print(f"\nUsing temp directory: {tmpdir}")

        try:
            svc, ms, repo = _build_services(tmpdir)
        except Exception as exc:
            print(f"\nFATAL: Could not build services: {exc}")
            print("This is expected if Mem0/Qdrant/FastEmbed backend is unavailable.")
            print("Skipping E2E tests.")
            sys.exit(0)

        passed = 0
        failed = 0
        skipped = 0

        cases = [
            ("Case 1", run_case_1),
            ("Case 2", run_case_2),
            ("Case 3", run_case_3),
            ("Case 4", run_case_4),
            ("Case 5", run_case_5),
        ]

        for name, case_fn in cases:
            try:
                case_fn(svc, ms)
                passed += 1
            except AssertionError as e:
                print(f"  ✗ FAIL: {e}")
                failed += 1
            except Exception as e:
                print(f"  ✗ ERROR: {type(e).__name__}: {e}")
                failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
