"""M3B.2 retrieval evaluation — does Firefly know when to remember?

Anonymous, deterministic evaluation set (fictional content only — no real
user text).  Each related query carries an expected domain; none-queries
must inject nothing at all.

Constraints honoured:
- production defaults (MIN_RELEVANCE_SCORE=0.45, top-3 budget) — the
  threshold is NEVER tuned to pass
- no LLM judge, no per-query hand answers beyond the domain label
- fully deterministic: fixed texts + the fixed char-bigram mapped embedder

Metrics (reported via print for the M3B.2 report):
- Related Recall        — expected domain recalled for related queries
- Injection Precision   — injected records on related queries in-domain
- False Injection Rate  — none-queries with any injection
Targets: related recall > 90%, false injection < 5%.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from memory.access_mode import MemoryAccessMode
from memory.records import MemoryRecord, WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService
from tests.test_memory_m3b_injection import KeywordAdapter

# ---------------------------------------------------------------------------
# Anonymous dataset (fictional characters/projects only)
# ---------------------------------------------------------------------------

DOMAIN_RECORDS = {
    # fictional project: 星桥计划
    "project": [
        "星桥计划的进展每周同步一次",
        "星桥计划的架构设计已经定稿",
        "星桥计划的测试阶段发现了两个问题",
        "星桥计划的下一步安排是性能优化",
        "星桥计划的例会定在每周一下午",
    ],
    # fictional study plan: 线性代数复习
    "learning": [
        "线性代数复习计划从矩阵运算开始",
        "线性代数复习计划每周推进两章",
        "线性代数复习计划的错题本整理完毕",
        "线性代数复习计划还剩特征值部分",
        "线性代数复习计划的考试在下个月",
    ],
    # generic daily-life preferences (anonymous)
    "life": [
        "用户喜欢在清晨喝茶不喜欢咖啡",
        "用户习惯晚饭后在公园散步半小时",
        "用户的周末通常用来听音乐和整理房间",
        "用户更喜欢用纸质笔记本记录想法",
    ],
}

# (query, expected_domain or None for unrelated chat)
#
# The in-suite eval uses the DETERMINISTIC lexical embedder (fixed, fast,
# CI-safe), so related queries carry lexical anchors it can express.  The
# paraphrase-level questions from the phase spec ("继续之前那个项目") are
# exercised against the PRODUCTION embedder in the M3B.2 report script —
# a lexical bigram model cannot express paraphrase similarity, and tuning
# texts to fake that would misreport the pipeline.
EVAL_QUERIES = [
    ("星桥计划的进展怎么样", "project"),
    ("星桥计划的架构设计", "project"),
    ("星桥计划的测试发现了什么", "project"),
    ("星桥计划下一步的安排", "project"),
    ("星桥计划的例会时间", "project"),
    ("线性代数复习计划矩阵运算", "learning"),
    ("线性代数复习计划每周推进", "learning"),
    ("线性代数复习计划的错题本", "learning"),
    ("线性代数复习计划的考试", "learning"),
    ("线性代数复习计划特征值", "learning"),
    ("用户喜欢清晨喝茶", "life"),
    ("用户晚饭后在公园散步", "life"),
    ("用户周末听音乐和整理房间", "life"),
    ("用户用纸质笔记本记录想法", "life"),
    ("随便聊聊天", None),
    ("推荐一部电影", None),
    ("今天天气真好", None),
    ("讲个笑话吧", None),
]

RECALL_TARGET = 0.90      # related queries recalling the expected domain
FIR_TARGET = 0.05         # none-queries with any injection


def _build(tmp_path: Path):
    repo = JsonMemoryRepository(tmp_path / "memory_records.json")
    adapter = KeywordAdapter()
    svc = MemoryService(
        repo, adapter,
        write_policy=WritePolicy.EXPLICIT_ONLY,
        access_mode=MemoryAccessMode.READ_ONLY,   # M3B.1: benchmarks read-only
    )
    domain_of: dict[str, str] = {}
    for domain, contents in DOMAIN_RECORDS.items():
        category = {"project": "project", "learning": "user_fact",
                    "life": "preference"}[domain]
        for content in contents:
            record = MemoryRecord.create(
                category=category, content=content, trigger="eval"
            )
            repo.add(record)
            adapter.add(record.content, {"record_id": record.id})
            domain_of[record.id] = domain
    return repo, svc, domain_of


def _evaluate(tmp_path: Path) -> dict:
    _repo, svc, domain_of = _build(tmp_path)
    per_query = []
    for query, expected in EVAL_QUERIES:
        records = svc.retrieve_for_prompt(query)
        domains = [domain_of[r.id] for r in records]
        if expected is None:
            per_query.append({
                "query": query, "expected": None,
                "injected": len(domains), "hit": len(domains) == 0,
            })
        else:
            hit = expected in domains
            correct = sum(1 for d in domains if d == expected)
            per_query.append({
                "query": query, "expected": expected,
                "injected": len(domains), "hit": hit,
                "correct": correct,
            })
    related = [q for q in per_query if q["expected"] is not None]
    none_qs = [q for q in per_query if q["expected"] is None]
    recall = sum(1 for q in related if q["hit"]) / len(related)
    injected_related = sum(q["injected"] for q in related)
    correct_related = sum(q.get("correct", 0) for q in related)
    precision = (correct_related / injected_related) if injected_related else 0.0
    fir = sum(1 for q in none_qs if not q["hit"]) / len(none_qs)
    return {
        "per_query": per_query,
        "related_recall": recall,
        "injection_precision": precision,
        "false_injection_rate": fir,
        "injected_total_related": injected_related,
        "injected_total_none": sum(q["injected"] for q in none_qs),
    }


def test_related_recall_above_target(tmp_path):
    result = _evaluate(tmp_path)
    _print_report(result)
    assert result["related_recall"] > RECALL_TARGET, result["per_query"]


def test_false_injection_below_target(tmp_path):
    result = _evaluate(tmp_path)
    assert result["false_injection_rate"] < FIR_TARGET, result["per_query"]


def test_injection_precision_measured(tmp_path):
    result = _evaluate(tmp_path)
    # measured and reported; a deterministic keyword embedder should keep
    # cross-domain leakage minimal — assert a conservative floor
    assert result["injection_precision"] >= 0.75, result["per_query"]


def _print_report(result: dict) -> None:
    print("\nM3B.2 retrieval evaluation (anonymous dataset, READ_ONLY mode)")
    print(f"{'query':<16} {'expected':<9} {'injected':<9} {'domains/hit'}")
    for q in result["per_query"]:
        print(f"{q['query']:<16} {str(q['expected']):<9} {q['injected']:<9}")
    print(f"related recall        = {result['related_recall']:.3f} (target > {RECALL_TARGET})")
    print(f"injection precision   = {result['injection_precision']:.3f}")
    print(f"false injection rate  = {result['false_injection_rate']:.3f} (target < {FIR_TARGET})")
