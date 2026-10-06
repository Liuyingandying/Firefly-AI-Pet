# -*- coding: utf-8 -*-
"""Concept approval schema — 概念批准桥协议（M2.6）。

设计约束:
- 输入只读消费 M2.5 ``ConceptProposal``; 输出构造 M1.2 ``ConceptNode``
  **对象**（规则 5: 转换结果为对象返回, 本包永不持有/写入图实例）;
- **零自动批准**（规则 2）: 无显式 ``approved_ids`` 即拒绝, 门是硬的;
- 规约五字段 + 一个溯源扩展 ``provenance``（规则 3: 保留 proposal 溯源——
  ``ConceptNode`` 本体禁改无溯源字段, 溯源随批准结果交付）;
- 全字段 JSON 原生类型; 门/输入问题走 error, 逐提案问题走 rejected。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 拒绝原因封闭词表（rejected[].reason）
# ---------------------------------------------------------------------------

REJECT_NOT_APPROVED = "not_approved"            # 未列入 approved_ids（跳过）
REJECT_INVALID_ID = "invalid_id"                # id 空/含空白/非法字符/超长
REJECT_DUPLICATE = "duplicate"                  # 同批重复 id
REJECT_CYCLE = "prerequisite_cycle"             # 前置闭环成员

# ---------------------------------------------------------------------------
# 失败码（error 字段, 门/输入级）
# ---------------------------------------------------------------------------

ERR_EXPLICIT_APPROVAL_REQUIRED = "explicit_approval_required"  # 规则 2
ERR_INVALID_INPUT = "invalid_input"

#: 合法 concept id 形态: 非空、无空白、≤64、受限字符集（词/点/冒号/连字符/下划线）
import re as _re

_ID_RE = _re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")


def is_valid_concept_id(concept_id: str) -> bool:
    """规则 4 的 id 合法性判定（纯函数, 供 approver 与测试共用）。"""
    return bool(concept_id) and bool(_ID_RE.match(concept_id))


@dataclass(frozen=True)
class ApprovalResult:
    """一次显式批准转换的完整结果（门级失败与逐提案拒绝可区分）。"""

    approved: bool                              # True = 显式批准门已通过
    created_nodes: tuple = ()                   # tuple[ConceptNode, ...]（对象, 不写图）
    rejected: tuple[dict, ...] = ()             # [{"concept_id", "reason"}]
    warnings: tuple[str, ...] = ()
    error: str = ""                             # 门/输入级失败时非空
    #: 规则 3 扩展: concept_id → proposal 溯源（source_candidate_id/level/chapter/kind）
    provenance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "created_nodes": [node.to_dict() for node in self.created_nodes],
            "rejected": [dict(r) for r in self.rejected],
            "warnings": list(self.warnings),
            "error": self.error,
            "provenance": dict(self.provenance),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
