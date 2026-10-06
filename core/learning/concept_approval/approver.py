# -*- coding: utf-8 -*-
"""ConceptApprover — ConceptProposal → ConceptNode 显式确认桥（M2.6）。

    ConceptProposal(M2.5) × N ──approved_ids（显式, 规则 1/2）──▶ ApprovalResult
                                            │
                                            └─▶ created_nodes: ConceptNode 对象
                                                （规则 5: 对象返回, 调用方构造注入图）

五条规则（M2.6 验收口径）:
1. **只有显式 approved_ids 才转换**: 未列入者跳过（rejected/not_approved）;
2. **拒绝自动批准**: approved_ids 缺失/为空 → approved=False + 门级错误;
3. **保留 proposal 溯源**: 节点的 chapter 取 proposal.source_chapter,
   全量溯源随 result.provenance 交付（ConceptNode 本体禁改, 溯源外挂）;
4. **构造前三检查**: id 合法（形态/长度）→ 前置无环（建节点子图 DFS）→
   实验存在（可选 experiment_ids 白名单, 违者剥离引用并告警）;
5. **转换结果为对象返回, 不写 Graph**: 零图实例交互。

逐提案失败（非法 id/重复/环/未批准）走 rejected, 不阻断其余批准;
仅门级（未显式批准）与输入级（非提案集合）失败走 error + approved=False。
"""

from __future__ import annotations

from core.learning.concept_approval.schema import (
    ERR_EXPLICIT_APPROVAL_REQUIRED,
    ERR_INVALID_INPUT,
    REJECT_CYCLE,
    REJECT_DUPLICATE,
    REJECT_INVALID_ID,
    REJECT_NOT_APPROVED,
    ApprovalResult,
    is_valid_concept_id,
)
from core.learning.concept_refinement.schema import ConceptProposal


class ConceptApprover:
    """概念批准桥（显式门 + 三检查 + 对象交付, 零图写入）。"""

    def approve(
        self,
        proposals,
        *,
        approved_ids: set[str] | frozenset[str] | list[str] | tuple[str, ...],
        experiment_ids: set[str] | frozenset[str] | list[str] | None = None,
        known_concept_ids: set[str] | frozenset[str] | list[str] | None = None,
    ) -> ApprovalResult:
        """显式批准转换; 门级/输入级失败返回 approved=False, 永不抛异常。"""
        # ---- 输入归一 ---------------------------------------------------------
        if proposals is None or isinstance(proposals, ConceptProposal):
            proposal_list = [proposals] if isinstance(proposals, ConceptProposal) else None
        elif isinstance(proposals, (list, tuple)):
            proposal_list = list(proposals)
        else:
            proposal_list = None
        if proposal_list is None or any(
            not isinstance(p, ConceptProposal) for p in proposal_list
        ):
            return ApprovalResult(
                approved=False,
                error=f"{ERR_INVALID_INPUT}: proposals must be ConceptProposal(s)",
            )

        # ---- 规则 1/2: 显式批准门（拒绝自动批准） ------------------------------
        approved_set = {str(a) for a in (approved_ids or ()) if str(a)}
        if not approved_set:
            return ApprovalResult(
                approved=False,
                rejected=tuple(
                    {"concept_id": getattr(p, "concept_id", ""), "reason": REJECT_NOT_APPROVED}
                    for p in proposal_list
                ),
                error=f"{ERR_EXPLICIT_APPROVAL_REQUIRED}: approved_ids is empty",
            )

        experiment_allow = {str(e) for e in experiment_ids} if experiment_ids is not None else None
        known_concepts = {str(c) for c in known_concept_ids} if known_concept_ids is not None else None

        rejected: list[dict] = []
        warnings: list[str] = []
        provenance: dict[str, dict] = {}
        survivors: list[ConceptProposal] = []
        seen: set[str] = set()

        # ---- 逐提案门内检查（id 合法 / 重复 / 未批准跳过） ----------------------
        for proposal in proposal_list:
            cid = proposal.concept_id
            if cid not in approved_set:
                rejected.append({"concept_id": cid, "reason": REJECT_NOT_APPROVED})
                continue
            if cid in seen:
                rejected.append({"concept_id": cid, "reason": REJECT_DUPLICATE})
                continue
            if not is_valid_concept_id(cid):
                rejected.append({"concept_id": cid, "reason": REJECT_INVALID_ID})
                continue
            seen.add(cid)
            survivors.append(proposal)

        # ---- 规则 4: 前置无环检查（在建节点子图上） -----------------------------
        prereq_by_id = {p.concept_id: tuple(p.prerequisites) for p in survivors}
        cyclic = _find_cycle_members(prereq_by_id)
        if cyclic:
            survivors = [p for p in survivors if p.concept_id not in cyclic]
            for cid in sorted(cyclic):
                rejected.append({"concept_id": cid, "reason": REJECT_CYCLE})
            warnings.append(
                f"prerequisite cycle rejected: {', '.join(sorted(cyclic))}"
            )

        # ---- 构造节点（实验存在检查 + 溯源外挂） --------------------------------
        from core.learning.knowledge_graph.schema import ConceptNode  # 只构造不写图

        created_ids = {p.concept_id for p in survivors}
        nodes: list = []
        for proposal in survivors:
            prerequisites = tuple(proposal.prerequisites)
            if known_concepts is not None:
                valid = (
                    created_ids
                    | known_concepts
                )
                kept = tuple(pr for pr in prerequisites if pr in valid)
                dropped = len(prerequisites) - len(kept)
                if dropped:
                    warnings.append(
                        f"dropped {dropped} unresolved prerequisite(s) of {proposal.concept_id}"
                    )
                prerequisites = kept

            related = tuple(proposal.related_experiments)
            if experiment_allow is not None:
                kept_exp = tuple(exp for exp in related if exp in experiment_allow)
                for exp in related:
                    if exp not in experiment_allow:
                        warnings.append(
                            f"unknown experiment stripped: {exp} (of {proposal.concept_id})"
                        )
                related = kept_exp

            nodes.append(
                ConceptNode(
                    concept_id=proposal.concept_id,
                    name=proposal.name,
                    chapter=proposal.source_chapter,       # 规则 3: 溯源复用
                    prerequisites=prerequisites,
                    related_experiments=related,
                )
            )
            provenance[proposal.concept_id] = {
                "source_candidate_id": proposal.source_candidate_id,
                "source_level": proposal.source_level,
                "source_chapter": proposal.source_chapter,
                "refinement_kind": proposal.refinement_kind,
            }

        return ApprovalResult(
            approved=True,
            created_nodes=tuple(nodes),
            rejected=tuple(rejected),
            warnings=tuple(warnings),
            provenance=provenance,
        )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------

def _find_cycle_members(prereq_by_id: dict[str, tuple[str, ...]]) -> set[str]:
    """在建节点子图上的前置闭环成员集合（无环返回空集）。"""
    state: dict[str, int] = {}          # 1=visiting 2=done
    stack: list[str] = []
    members: set[str] = set()

    def visit(cid: str) -> bool:
        status = state.get(cid)
        if status == 1:
            members.update(stack[stack.index(cid):])
            return True
        if status == 2:
            return False
        if cid not in prereq_by_id:      # 边可指向批准集外（已有图概念）, 不参与
            return False
        state[cid] = 1
        stack.append(cid)
        for prereq in prereq_by_id[cid]:
            if visit(prereq):
                state[cid] = 2
                stack.pop()
                return True
        stack.pop()
        state[cid] = 2
        return False

    for cid in list(prereq_by_id):
        visit(cid)
    return members
