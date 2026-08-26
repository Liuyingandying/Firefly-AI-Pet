"""Migration preview rendering (Stage 2).

Pure Python: no ``core``/``memory``/``character`` imports and no LLM. Renders
the human-reviewable ``migration_preview.json`` and the provenance
``Origin_Report.md`` from a ``MigrationCandidate``. It never writes runtime
data (no memory, bond, character, or conversation files).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .candidates import (
    BondCandidate,
    BondSignalType,
    MemoryCandidate,
    MemoryCategory,
    MigrationCandidate,
    StyleCandidate,
    StyleDimension,
)


_SENSITIVE_MEMORY = frozenset({MemoryCategory.EMOTION, MemoryCategory.RELATIONSHIP})

_SIGNAL_LABELS = {
    BondSignalType.TURN_COMPLETED: "一轮交流完成",
    BondSignalType.THANKED: "表达了感谢",
    BondSignalType.CORRECTION: "纠正了流萤",
    BondSignalType.SHARED_MILESTONE: "共同里程碑",
    BondSignalType.PROMISE_MADE: "做出约定",
    BondSignalType.PROMISE_KEPT: "兑现约定",
    BondSignalType.PROMISE_MISSED: "未兑现约定",
}

_DIMENSION_LABELS = {
    StyleDimension.NICKNAME: "称呼",
    StyleDimension.TONE: "语气",
    StyleDimension.VERBOSITY: "回复长度",
    StyleDimension.BOUNDARY: "边界",
}


def build_preview(migration: MigrationCandidate) -> dict[str, Any]:
    """Build the enriched ``migration_preview.json`` structure."""
    return {
        "run_id": migration.run_id,
        "source": migration.source,
        "source_file": migration.source_file,
        "rule_version": migration.rule_version,
        "summary": migration.summary(),
        "groups": {
            "memory": [_memory_item(item) for item in migration.memory],
            "bond": [_bond_item(item) for item in migration.bond],
            "style": [_style_item(item) for item in migration.style],
        },
    }


def write_preview(migration: MigrationCandidate, path: str | Path) -> None:
    """Write the enriched preview as ``migration_preview.json``."""
    _write_json(build_preview(migration), path)


def render_origin_report(migration: MigrationCandidate) -> str:
    """Render the human-readable provenance report (Markdown)."""
    summary = migration.summary()
    lines = [
        "# Firefly 历史迁移溯源报告",
        "",
        f"- **run_id**: `{migration.run_id}`",
        f"- **source**: {migration.source}",
        f"- **source_file**: {migration.source_file}",
        f"- **rule_version**: {migration.rule_version}",
        "",
        "## 概要",
        "",
        "| 类别 | 总数 | 自动通过 | 需人工确认 | 自动拒绝 |",
        "|------|------|---------|-----------|---------|",
        _summary_row("Memory", summary["memory"]),
        _summary_row("Bond", summary["bond"]),
        _summary_row("Style", summary["style"]),
        "",
    ]
    lines.extend(_memory_section(migration.memory))
    lines.extend(_bond_section(migration.bond))
    lines.extend(_style_section(migration.style))
    return "\n".join(lines)


def write_origin_report(migration: MigrationCandidate, path: str | Path) -> None:
    """Write the provenance report to ``Origin_Report.md``."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_origin_report(migration) + "\n", encoding="utf-8")


def _memory_item(candidate: MemoryCandidate) -> dict[str, Any]:
    item = candidate.to_dict()
    item["risk"] = _risk(candidate)
    item["preview"] = _preview_text(candidate)
    item["review"] = "pending"
    return item


def _bond_item(candidate: BondCandidate) -> dict[str, Any]:
    item = candidate.to_dict()
    item["risk"] = _risk(candidate)
    item["preview"] = _preview_text(candidate)
    item["review"] = "pending"
    return item


def _style_item(candidate: StyleCandidate) -> dict[str, Any]:
    item = candidate.to_dict()
    item["risk"] = _risk(candidate)
    item["preview"] = _preview_text(candidate)
    item["review"] = "pending"
    return item


def _risk(candidate: Any) -> list[str]:
    risks = list(candidate.flags)
    if (
        isinstance(candidate, MemoryCandidate)
        and candidate.category in _SENSITIVE_MEMORY
        and "sensitive" not in risks
    ):
        risks.append("sensitive")
    return risks


def _preview_text(candidate: Any) -> str:
    if isinstance(candidate, MemoryCandidate):
        return f"你在 {_when(candidate)} 说：「{candidate.content}」"
    if isinstance(candidate, StyleCandidate):
        label = _DIMENSION_LABELS.get(candidate.dimension, candidate.dimension.value)
        return f"你偏好{label}：「{candidate.content}」"
    label = _SIGNAL_LABELS.get(candidate.signal_type, candidate.signal_type.value)
    if candidate.detail:
        return f"{label}：「{candidate.detail}」"
    return label


def _when(candidate: Any) -> str:
    if candidate.evidence:
        return candidate.evidence[0].ts or "某时"
    return "某时"


def _summary_row(label: str, counts: dict[str, int]) -> str:
    return (
        f"| {label} | {counts['total']} | {counts['auto_approve']} | "
        f"{counts['needs_review']} | {counts['auto_reject']} |"
    )


def _memory_section(items: list[MemoryCandidate]) -> list[str]:
    if not items:
        return []
    lines = ["## Memory 候选", ""]
    for candidate in items:
        lines += [
            f"### {candidate.id} · {candidate.category.value} · "
            f"{candidate.band.value} · {candidate.disposition.value}",
            "",
            f"- 内容：`{candidate.content}`",
            f"- 置信度：{candidate.confidence}",
            f"- 风险：{_risk_text(candidate)}",
            f"- 规则：`{candidate.rule}`",
        ]
        lines += _evidence_lines(candidate)
        lines.append("")
    return lines


def _bond_section(items: list[BondCandidate]) -> list[str]:
    if not items:
        return []
    lines = ["## Bond 候选", ""]
    for candidate in items:
        detail = candidate.detail or ""
        heading = (
            f"### {candidate.id} · {candidate.signal_type.value}"
            f"{(' · ' + detail) if detail else ''} · "
            f"{candidate.band.value} · {candidate.disposition.value}"
        )
        lines += [
            heading,
            "",
            f"- 置信度：{candidate.confidence}",
            f"- 风险：{_risk_text(candidate)}",
            f"- 规则：`{candidate.rule}`",
        ]
        lines += _evidence_lines(candidate)
        lines.append("")
    return lines


def _style_section(items: list[StyleCandidate]) -> list[str]:
    if not items:
        return []
    lines = ["## Style 候选", ""]
    for candidate in items:
        lines += [
            f"### {candidate.id} · {candidate.dimension.value} · "
            f"{candidate.band.value} · {candidate.disposition.value}",
            "",
            f"- 内容：`{candidate.content}`",
            f"- 置信度：{candidate.confidence}",
            f"- 风险：{_risk_text(candidate)}",
            f"- 规则：`{candidate.rule}`",
            f"- review_only：{candidate.review_only}",
        ]
        lines += _evidence_lines(candidate)
        lines.append("")
    return lines


def _evidence_lines(candidate: Any) -> list[str]:
    lines = ["- 来源："]
    for ref in candidate.evidence:
        lines.append(
            f"  - `{ref.conversation_id}` · turn {ref.turn_seq} · "
            f"{ref.ts or '未知时间'} · {ref.role.value} · "
            f"message_id `{ref.message_id or '-'}`"
        )
    return lines


def _risk_text(candidate: Any) -> str:
    risks = _risk(candidate)
    return "、".join(risks) if risks else "无"


def _write_json(data: dict[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


__all__ = [
    "build_preview",
    "render_origin_report",
    "write_origin_report",
    "write_preview",
]
