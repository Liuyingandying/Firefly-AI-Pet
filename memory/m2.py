"""Memory M2A — dedup / conflict / lifecycle policy (read-only core).

Layering (mirrors the Learning domain's "LLM evidence → rule decision"):

    MemoryCandidate (resolved facts)
        ↓
    relationship classifier (deterministic; optional LLM *evidence* only)
        ↓
    MemoryWritePolicy.evaluate  ← the ONLY mutation decision point
        ↓
    MemoryWriteDecision  → MemoryService executes (dry-run safe)

Nothing in this module writes to the repository: it is a pure decision layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Iterable, Sequence

__all__ = [
    "MemoryKind",
    "Durability",
    "Relation",
    "WriteAction",
    "MemoryCandidate",
    "RelationEvidence",
    "MemoryWriteDecision",
    "MemoryWritePolicy",
    "MemoryDedupPlanner",
    "normalize_memory_text",
    "kind_from_category",
    "durability_for_kind",
    "classify_relation",
]


# ---------------------------------------------------------------------------
# Taxonomy (v1 — deliberately tiny)
# ---------------------------------------------------------------------------


class MemoryKind(str, Enum):
    """What kind of fact this memory is (orthogonal to MemoryCategory)."""

    PROFILE_FACT = "profile_fact"          # 相对稳定的个人事实
    PREFERENCE = "preference"              # 偏好/习惯
    GOAL = "goal"                          # 当前长期目标
    PROJECT_CONTEXT = "project_context"    # 持续项目事实
    EPISODIC = "episodic"                  # 发生过的事件
    TEMPORARY_STATE = "temporary_state"    # 短期状态
    OTHER = "other"


class Durability(str, Enum):
    DURABLE = "durable"
    CONTEXTUAL = "contextual"
    TEMPORARY = "temporary"


_KIND_FROM_CATEGORY: dict[str, MemoryKind] = {
    "user_fact": MemoryKind.PROFILE_FACT,
    "preference": MemoryKind.PREFERENCE,
    "project": MemoryKind.PROJECT_CONTEXT,
    "relationship": MemoryKind.PROFILE_FACT,
    "shared_experience": MemoryKind.EPISODIC,
    "emotion": MemoryKind.EPISODIC,
}

_KIND_DURABILITY: dict[MemoryKind, Durability] = {
    MemoryKind.PROFILE_FACT: Durability.DURABLE,
    MemoryKind.PREFERENCE: Durability.DURABLE,
    MemoryKind.GOAL: Durability.DURABLE,
    MemoryKind.PROJECT_CONTEXT: Durability.CONTEXTUAL,
    MemoryKind.EPISODIC: Durability.CONTEXTUAL,
    MemoryKind.TEMPORARY_STATE: Durability.TEMPORARY,
    MemoryKind.OTHER: Durability.CONTEXTUAL,
}


def kind_from_category(category: str | None) -> MemoryKind:
    """Map the existing MemoryCategory onto the M2A kind taxonomy."""
    try:
        return _KIND_FROM_CATEGORY.get(str(category or "").lower(), MemoryKind.OTHER)
    except Exception:  # noqa: BLE001
        return MemoryKind.OTHER


def durability_for_kind(kind: MemoryKind | str) -> Durability:
    try:
        kind = MemoryKind(kind)
    except ValueError:
        kind = MemoryKind.OTHER
    return _KIND_DURABILITY[kind]


# ---------------------------------------------------------------------------
# Safe normalization (Stage 1 exact check)
# ---------------------------------------------------------------------------

_PUNCT_MAP = {
    0xFF0C: ",", 0x3002: ".", 0xFF01: "!", 0xFF1F: "?",
    0xFF1B: ";", 0xFF1A: ":", 0x3001: ",", 0xFF08: "(",
    0xFF09: ")", 0x201C: '"', 0x201D: '"', 0x2018: "'",
    0x2019: "'", 0xFF5E: "~", 0x2014: "-", 0x2026: "...",
}


def normalize_memory_text(text: str) -> str:
    """Safe normalization only: trim, collapse whitespace, lightly unify
    CJK/latin punctuation, lowercase latin. No paraphrase, no stemming."""
    cleaned = (text or "").strip()
    cleaned = cleaned.translate(_PUNCT_MAP)
    cleaned = cleaned.casefold()
    return re.sub(r"[.,!?;:\- ~]+$", "", re.sub(r"\s+", " ", cleaned).strip()).strip()


# ---------------------------------------------------------------------------
# Candidate / relation / decision models
# ---------------------------------------------------------------------------


class Relation(str, Enum):
    SAME = "same"
    EXTENDS = "extends"
    CONFLICTS = "conflicts"
    RELATED = "related"
    UNRELATED = "unrelated"


class WriteAction(str, Enum):
    CREATE = "create"
    IGNORE_DUPLICATE = "ignore_duplicate"
    MERGE_UPDATE = "merge_update"
    SUPERSEDE = "supersede"
    KEEP_BOTH = "keep_both"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DO_NOT_PERSIST = "do_not_persist"  # 自动提取的 temporary state 默认丢弃


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A thin write-time candidate — resolved facts only, not a truth store."""

    content: str
    kind: MemoryKind = MemoryKind.OTHER
    source: str = "explicit_user"
    explicit: bool = True
    confidence: float = 1.0
    timestamp_ms: int | None = None
    session_id: str | None = None
    subject_key: str | None = None  # 冲突槽位标识；None → 不参与 supersede

    @property
    def durability(self) -> Durability:
        base = durability_for_kind(self.kind)
        if self.explicit:
            return base
        if base is Durability.DURABLE:
            return Durability.CONTEXTUAL
        return base


@dataclass(frozen=True, slots=True)
class RelationEvidence:
    """Structured evidence (LLM may produce this; it never mutates memory)."""

    relation: Relation
    confidence: float
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SubjectEvidence:
    """Lightweight subject identity evidence (deterministic / LLM / metadata)."""

    key: str | None
    confidence: float
    source: str  # deterministic | metadata | llm_evidence | unknown


@dataclass(frozen=True, slots=True)
class MemoryWriteDecision:
    action: WriteAction
    candidate: MemoryCandidate
    target_record_ids: tuple[str, ...] = ()
    relation: Relation | None = None
    confidence: float = 1.0
    reason: str = ""
    require_confirmation: bool = False
    supersede_reason: str = ""  # explicit_revision | explicit_correction | confirmed_conflict


# ---------------------------------------------------------------------------
# Relation classification (deterministic first; LLM evidence optional)
# ---------------------------------------------------------------------------

_CONFLICT_MARKERS = (
    "不喜欢", "不再", "决定不", "换成", "改成了", "取消", "放弃",
    "现在更喜欢", "改为", "不打算", "不想",
    # 时间/态度变更信号（配合同主题低相似度 → CONFLICTS）
    "决定", "改成", "换成", "改用", "现在",
)
_EXTEND_MARKERS = ("尤其", "特别", "更具体", "补充", "另外", "而且")

# M2C: temporal revision markers (boost CONFLICTS confidence when present)
_TEMPORAL_REVISION_MARKERS = (
    "现在", "改成", "不再", "后来", "决定", "目前", "以后", "从现在开始",
)

# M2C: negation patterns (negate the existing fact)
_NEGATION_PATTERNS = ("不喜欢", "不再喜欢", "不喜欢了", "不", "没有", "不搞")

# M2C: explicit correction patterns ("不是X是Y", "说错了", "更正")
_CORRECTION_PATTERNS = ("不是", "说错了", "更正", "纠正", "之前不对", "刚才说错")

# M2C: compatibility markers ("也喜欢" → compatible, not conflict)
_COMPATIBILITY_MARKERS = ("也喜欢", "也", "还想", "另外")


def _has_temporal_revision(text: str) -> bool:
    return any(m in (text or "") for m in _TEMPORAL_REVISION_MARKERS)


def _has_negation_flip(candidate: str, existing: str) -> bool:
    """Detect negation flip: one has negation, the other doesn't, same topic."""
    neg = _NEGATION_PATTERNS
    cand_neg = any(m in candidate for m in neg)
    exist_neg = any(m in existing for m in neg)
    return cand_neg != exist_neg


def _is_explicit_correction(text: str) -> bool:
    return any(m in (text or "") for m in _CORRECTION_PATTERNS)


def extract_subject_key(content: str, kind: MemoryKind) -> str | None:
    """Extract a lightweight subject key for conflict slot matching.

    M2C.1: returns ``kind.value`` as a COARSE slot identifier (all PREFERENCE
    memories share the same preference slot). The first-20 heuristic was
    proven unreliable by adversarial audit (11/20 false negatives) and has
    been replaced. Conflict specificity is provided by ``classify_relation``.
    """
    return kind.value


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def classify_relation(
    candidate_content: str,
    existing_content: str,
    *,
    candidate_kind: MemoryKind | None = None,
    existing_kind: MemoryKind | None = None,
    evidence: RelationEvidence | None = None,
) -> tuple[Relation, float]:
    """Deterministic relation between a candidate and an existing memory.

    An optional LLM :class:`RelationEvidence` can adjust confidence, but the
    returned relation stays grounded in deterministic checks unless the
    evidence agrees; conflicting low-confidence evidence degrades to RELATED.
    """
    left = normalize_memory_text(candidate_content)
    right = normalize_memory_text(existing_content)
    if not left or not right:
        return Relation.UNRELATED, 0.0
    if left == right:
        return Relation.SAME, 1.0
    ratio = _similarity(left, right)

    candidate_conflict = any(m in left for m in _CONFLICT_MARKERS)
    existing_conflict = any(m in right for m in _CONFLICT_MARKERS)
    kind_conflict = (
        candidate_kind is not None
        and existing_kind is not None
        and candidate_kind == existing_kind
    )

    if candidate_conflict != existing_conflict and ratio >= 0.35:
        # 一方带明确的否定/变更标记，另一方没有 → 冲突候选
        # Confidence >= 0.7：marker-based conflict 已是强信号。
        return Relation.CONFLICTS, max(0.7, ratio)

    if ratio >= 0.92:
        return Relation.SAME, ratio
    if any(m in left for m in _EXTEND_MARKERS) and right in left:
        return Relation.EXTENDS, max(0.6, ratio)
    if ratio >= 0.55:
        relation = Relation.RELATED if not kind_conflict else Relation.CONFLICTS
        confidence = max(0.55, ratio)
        if evidence is not None and evidence.relation is Relation.CONFLICTS and evidence.confidence >= 0.7:
            relation = Relation.CONFLICTS
            confidence = max(confidence, evidence.confidence)
        return relation, confidence
    if evidence is not None:
        # LLM 只能提供结构化证据；低置信度不改变确定性结果。
        if evidence.relation is Relation.CONFLICTS and evidence.confidence >= 0.7:
            return Relation.CONFLICTS, evidence.confidence
        if evidence.relation is Relation.UNRELATED and evidence.confidence >= 0.7:
            return Relation.UNRELATED, evidence.confidence
    return Relation.UNRELATED, ratio


# ---------------------------------------------------------------------------
# MemoryWritePolicy — the only mutation decision point
# ---------------------------------------------------------------------------


class MemoryWritePolicy:
    """Deterministic write policy: candidate + existing records → decision.

    The policy never writes; :class:`~memory.service.MemoryService` executes
    the returned :class:`MemoryWriteDecision` (dry-run safe by construction).
    """

    NAME = "memory-write-policy-v2"
    EXTENDS_MERGE_RATIO = 0.85     # deterministic-safe merge threshold
    CONFLICT_CONFIDENCE = 0.7      # below → REQUIRE_CONFIRMATION
    RELATED_KEEP_RATIO = 0.55
    SUBJECT_CONFIDENCE = 0.7       # below → REQUIRE_CONFIRMATION (gate 4)

    def evaluate(
        self,
        candidate: MemoryCandidate,
        existing_records: Sequence[Any],
        *,
        evidence: RelationEvidence | None = None,
    ) -> MemoryWriteDecision:
        existing_records = [r for r in existing_records if getattr(r, "lifecycle_status", "active") != "superseded"]

        # 0) durability gate: 自动提取的 temporary state 默认不进入长期记忆。
        if (
            candidate.durability is Durability.TEMPORARY
            and not candidate.explicit
        ):
            return MemoryWriteDecision(
                action=WriteAction.DO_NOT_PERSIST,
                candidate=candidate,
                relation=Relation.UNRELATED,
                confidence=candidate.confidence,
                reason="temporary state from automatic extraction",
            )

        # 1) deterministic exact-normalized duplicate check (highest priority).
        normalized = normalize_memory_text(candidate.content)
        for record in existing_records:
            if normalize_memory_text(getattr(record, "content", "")) == normalized:
                return MemoryWriteDecision(
                    action=WriteAction.IGNORE_DUPLICATE,
                    candidate=candidate,
                    target_record_ids=(getattr(record, "id", ""),),
                    relation=Relation.SAME,
                    confidence=1.0,
                    reason="exact normalized duplicate (reinforced, not re-added)",
                )

        # 2) semantic candidate discovery + relation classification.
        best_id, best_relation, best_conf = None, Relation.UNRELATED, 0.0
        for record in existing_records:
            relation, confidence = classify_relation(
                candidate.content,
                getattr(record, "content", "") or "",
                candidate_kind=candidate.kind,
                existing_kind=kind_from_category(getattr(record, "category", None)),
                evidence=evidence,
            )
            if confidence > best_conf:
                best_id = getattr(record, "id", "")
                best_relation = relation
                best_conf = confidence

        if best_id is None or best_relation is Relation.UNRELATED:
            return MemoryWriteDecision(
                action=WriteAction.CREATE,
                candidate=candidate,
                relation=Relation.UNRELATED,
                confidence=candidate.confidence,
                reason="independent fact",
            )

        targets = (best_id,)
        if best_relation is Relation.SAME:
            return MemoryWriteDecision(
                action=WriteAction.IGNORE_DUPLICATE,
                candidate=candidate,
                target_record_ids=targets,
                relation=best_relation,
                confidence=best_conf,
                reason="same content (normalized similarity)",
            )

        if best_relation is Relation.EXTENDS:
            left = normalize_memory_text(candidate.content)
            right = normalize_memory_text(
                next(
                    (getattr(r, "content", "") for r in existing_records if getattr(r, "id", "") == best_id),
                    "",
                )
            )
            if best_conf >= self.EXTENDS_MERGE_RATIO and right and right in left:
                # deterministic-safe merge：existing 是 candidate 的纯前缀子集。
                return MemoryWriteDecision(
                    action=WriteAction.MERGE_UPDATE,
                    candidate=candidate,
                    target_record_ids=targets,
                    relation=best_relation,
                    confidence=best_conf,
                    reason="deterministic safe merge (existing is prefix of candidate)",
                )
            return MemoryWriteDecision(
                action=WriteAction.KEEP_BOTH,
                candidate=candidate,
                target_record_ids=targets,
                relation=best_relation,
                confidence=best_conf,
                reason="complementary update — conservative keep both",
            )

        if best_relation is Relation.CONFLICTS:
            # EPISODIC → KEEP_BOTH（事件发生过就不被替代）。
            if candidate.kind is MemoryKind.EPISODIC:
                return MemoryWriteDecision(
                    action=WriteAction.KEEP_BOTH,
                    candidate=candidate,
                    target_record_ids=targets,
                    relation=best_relation,
                    confidence=best_conf,
                    reason="episodic facts are never superseded",
                )

            # Subject slot: same kind → same slot; different kind → different slot
            existing_rec = next(
                (r for r in existing_records if getattr(r, "id", "") == best_id),
                None,
            )
            existing_kind = (
                kind_from_category(getattr(existing_rec, "category", None))
                if existing_rec is not None
                else None
            )
            same_slot = (
                candidate.kind is not None
                and existing_kind is not None
                and candidate.kind is existing_kind
            )

            # M2C SUPERSEDE safety gates (ALL must pass):
            # Gate 1: explicit user memory write
            gate1 = candidate.explicit
            # Gate 2: conflict/revision marker present
            has_marker = any(
                m in normalize_memory_text(candidate.content) for m in _CONFLICT_MARKERS
            )
            is_correction = _is_explicit_correction(candidate.content)
            gate2 = has_marker or is_correction
            # Gate 3: relation confidence >= threshold
            gate3 = best_conf >= self.CONFLICT_CONFIDENCE
            # Gate 4: same subject slot; skip when either kind is OTHER/unknown
            gate4 = (
                candidate.kind is MemoryKind.OTHER
                or existing_kind is MemoryKind.OTHER
                or candidate.kind is existing_kind
            )
            # Gate 5: exactly one active target
            gate5 = len(targets) == 1
            evidence_conf = evidence.confidence if evidence is not None else 0.0

            if gate1 and gate2 and gate3 and gate4 and gate5:
                sup_reason = (
                    "explicit_correction" if is_correction else "explicit_revision"
                )
                return MemoryWriteDecision(
                    action=WriteAction.SUPERSEDE,
                    candidate=candidate,
                    target_record_ids=targets,
                    relation=best_relation,
                    confidence=max(best_conf, evidence_conf),
                    reason="all supersede gates passed",
                    supersede_reason=sup_reason,
                )
            return MemoryWriteDecision(
                action=WriteAction.REQUIRE_CONFIRMATION,
                candidate=candidate,
                target_record_ids=targets,
                relation=best_relation,
                confidence=max(best_conf, evidence_conf),
                reason="supersede gates not all passed — needs confirmation",
                require_confirmation=True,
            )

        if best_conf < self.RELATED_KEEP_RATIO:
            return MemoryWriteDecision(
                action=WriteAction.CREATE,
                candidate=candidate,
                target_record_ids=targets,
                relation=best_relation,
                confidence=best_conf,
                reason="weak relation — keep as independent",
            )
        return MemoryWriteDecision(
            action=WriteAction.KEEP_BOTH,
            candidate=candidate,
            target_record_ids=targets,
            relation=best_relation,
            confidence=best_conf,
            reason="related but not mergeable — keep both",
        )


# ---------------------------------------------------------------------------
# Planner (read-only dry-run over real records)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanEntry:
    record_id: str
    action: WriteAction
    canonical_id: str | None = None
    relation: Relation | None = None
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class DedupPlan:
    entries: tuple[PlanEntry, ...] = ()
    statistics: dict[str, int] = field(default_factory=dict)
    canonical_ids: tuple[str, ...] = ()


class MemoryDedupPlanner:
    """Read-only dedup planner over existing records (dry-run; no mutation).

    Deterministic canonical selection per duplicate group:
        1. richest provenance (vector_id present, longer content)
        2. healthy canonical id (no dangling supersede)
        3. earliest created_at
        4. record id (final tie-break)
    """

    def __init__(self, policy: MemoryWritePolicy | None = None) -> None:
        self.policy = policy or MemoryWritePolicy()

    def plan(self, records: Sequence[Any]) -> DedupPlan:
        entries: list[PlanEntry] = []
        groups: dict[str, list[Any]] = {}
        for record in records:
            key = hashlib_sha(normalize_memory_text(getattr(record, "content", "")))
            groups.setdefault(key, []).append(record)

        for _key, members in sorted(groups.items()):
            members = sorted(
                members,
                key=lambda r: (
                    0 if getattr(r, "vector_id", None) else 1,
                    getattr(r, "created_ts", 0),
                    getattr(r, "id", ""),
                ),
            )
            canonical = members[0]
            entries.append(
                PlanEntry(
                    record_id=canonical.id,
                    action=WriteAction.CREATE,  # canonical 保留
                    canonical_id=canonical.id,
                )
            )
            for duplicate in members[1:]:
                entries.append(
                    PlanEntry(
                        record_id=duplicate.id,
                        action=WriteAction.IGNORE_DUPLICATE,
                        canonical_id=canonical.id,
                        relation=Relation.SAME,
                        confidence=1.0,
                    )
                )

        statistics = _count_actions(entries)
        canonical_ids = tuple(e.record_id for e in entries if e.action is WriteAction.CREATE)
        return DedupPlan(entries=tuple(entries), statistics=statistics, canonical_ids=canonical_ids)


def _count_actions(entries: Iterable[PlanEntry]) -> dict[str, int]:
    stats: dict[str, int] = {}
    for entry in entries:
        stats[entry.action.value] = stats.get(entry.action.value, 0) + 1
    return stats


def hashlib_sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()