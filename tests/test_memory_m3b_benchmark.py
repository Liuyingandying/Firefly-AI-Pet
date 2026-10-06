"""M3B synthetic benchmark — M3B ranking vs raw semantic top-k baseline.

Fictional-character dataset (张三 / 李四), deterministic char-bigram
"semantic" adapter.  Measures Precision@3, Recall@3, False-Positive Rate
and zero-memory correctness for:

- NEW:  ``MemoryService.retrieve_for_prompt`` (threshold + diversity +
  superseded exclusion + injection budget)
- BASE: raw semantic top-3 with no lifecycle filter / threshold

The fake embedder scores char-bigram Jaccard similarity — it cannot
discriminate two people talking about the same topic, so the dataset uses
different-topic distractors.  The benchmark measures PIPELINE behaviour
(threshold / diversity / lifecycle), not embedding quality.

Run with ``pytest -s`` to print the per-query metric table for the M3B
report.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.test_memory_m3b_injection import (
    KeywordAdapter, _similarity,
)
from memory.records import MemoryRecord, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService


# ---------------------------------------------------------------------------
# Dataset (fictional, deterministic)
# ---------------------------------------------------------------------------

# (key, content, category, superseded_by_key)
DATASET = [
    ("r1", "张三最喜欢喝的咖啡是拿铁", "preference", "r2"),
    ("r2", "张三现在最喜欢喝的咖啡是澳白", "preference", None),
    ("r3", "张三在上海读研究生", "user_fact", None),
    ("r4", "张三的研究方向是视觉SLAM", "project", "r5"),
    ("r5", "张三的研究方向改为激光SLAM", "project", None),
    ("r6", "张三养了一只橘猫", "user_fact", None),
    ("r7", "张三的毕业设计答辩安排在下周三", "project", None),
    ("r8", "李四的家乡是成都", "user_fact", None),
    ("r9", "李四上周爬泰山很开心", "emotion", None),
]

# query -> ground-truth relevant record keys (active records only)
QUERIES = {
    "张三最喜欢喝的咖啡": ["r2"],          # r1 superseded duplicate exists
    "张三在上海读研究生吗": ["r3"],
    "张三的研究方向": ["r5"],              # r4 superseded duplicate exists
    "推荐一部好看的电影": [],               # unrelated chat → zero memories
}


def _build(tmp_path: Path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter = KeywordAdapter()
    service = MemoryService(repo, adapter, write_policy=WritePolicy.EXPLICIT_ONLY)
    ids: dict[str, str] = {}
    for key, content, category, _sup in DATASET:
        record = MemoryRecord.create(
            category=category, content=content, trigger="benchmark"
        )
        repo.add(record)
        adapter.add(record.content, {"record_id": record.id})
        ids[key] = record.id
    for key, _content, _category, sup in DATASET:
        if sup:
            repo.update(ids[key], {
                "lifecycle_status": "superseded",
                "superseded_by": ids[sup],
            })
    return repo, adapter, service, ids


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _metrics(retrieved: list[str], relevant: list[str]) -> dict:
    ret, rel = set(retrieved), set(relevant)
    tp = len(ret & rel)
    return {
        "tp": tp,
        "precision": (tp / len(ret)) if ret else 0.0,
        "recall": (tp / len(rel)) if rel else (1.0 if not ret else 0.0),
        "fp": len(ret - rel),
        "ret_size": len(ret),
        "zero_ok": (len(ret) == 0) if not rel else None,
    }


def _aggregate(per_query: dict) -> dict:
    ret_total = sum(m["ret_size"] for m in per_query.values())
    tp_total = sum(m["tp"] for m in per_query.values())
    fp_total = sum(m["fp"] for m in per_query.values())
    recalls = [m["recall"] for m in per_query.values() if m["zero_ok"] is None]
    zeros = [m["zero_ok"] for m in per_query.values() if m["zero_ok"] is not None]
    return {
        "precision": tp_total / ret_total if ret_total else 0.0,
        "recall": sum(recalls) / len(recalls) if recalls else 1.0,
        "fpr": fp_total / ret_total if ret_total else 0.0,
        "zero_memory_correct": all(zeros) if zeros else True,
    }


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def test_m3b_benchmark_beats_raw_topk_baseline(tmp_path):
    repo, adapter, service, ids = _build(tmp_path)

    new_per_query: dict[str, dict] = {}
    base_per_query: dict[str, dict] = {}

    for query, relevant_keys in QUERIES.items():
        relevant = [ids[k] for k in relevant_keys]

        # NEW: full M3B pipeline
        records = service.retrieve_for_prompt(query)
        new_per_query[query] = _metrics(
            [r.id for r in records], relevant
        )

        # BASE: raw semantic top-3, resolve to records, no lifecycle filter,
        # no threshold, no diversity
        hits = adapter.search(query, limit=3)
        base_ids: list[str] = []
        for hit in hits:
            rid = hit.metadata.get("record_id")
            if rid and repo.get(rid) is not None and rid not in base_ids:
                base_ids.append(rid)
        base_per_query[query] = _metrics(base_ids, relevant)

    new = _aggregate(new_per_query)
    base = _aggregate(base_per_query)

    print("\nM3B benchmark (张三/李四 synthetic dataset)")
    print(f"{'query':<14} {'NEW p/r/fp':<16} {'BASE p/r/fp':<16}")
    for query in QUERIES:
        n, b = new_per_query[query], base_per_query[query]
        print(
            f"{query:<14} "
            f"{n['precision']:.2f}/{n['recall']:.2f}/{n['fp']}      "
            f"{b['precision']:.2f}/{b['recall']:.2f}/{b['fp']}"
        )
    print(f"aggregate  NEW: {new}")
    print(f"aggregate BASE: {base}")

    # New pipeline gets every relevant active record the baseline gets,
    # without the noise
    assert new["recall"] >= base["recall"]
    assert new["precision"] > base["precision"]
    assert new["fpr"] == 0.0
    assert base["fpr"] > 0.0
    assert new["zero_memory_correct"] is True
    assert base["zero_memory_correct"] is False

    # Per-query spot checks: superseded duplicate never recalled, unrelated
    # chat injects nothing
    assert new_per_query["张三最喜欢喝的咖啡"]["fp"] == 0
    assert new_per_query["张三的研究方向"]["fp"] == 0
    assert new_per_query["推荐一部好看的电影"]["ret_size"] == 0


def test_m3b_benchmark_superseded_never_recalled_while_active_recalled(tmp_path):
    repo, adapter, service, ids = _build(tmp_path)
    records = service.retrieve_for_prompt("张三的研究方向")
    contents = [r.content for r in records]
    # active canonical first…
    assert contents[0] == "张三的研究方向改为激光SLAM"
    # …and the superseded twin never appears, even though it IS the top raw
    # semantic hit — the pipeline, not the embedder, removed it
    assert "张三的研究方向是视觉SLAM" not in contents
    raw_top1 = adapter.search("张三的研究方向", limit=1)[0]
    assert _similarity("张三的研究方向", raw_top1.text) > 0.4
