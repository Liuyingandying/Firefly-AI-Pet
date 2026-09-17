"""Authoritative orchestration for explicit Firefly memory operations."""

from __future__ import annotations

import logging

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol

from .mem0_adapter import Hit, Mem0Adapter
from .access_mode import MemoryAccessMode, MemoryAccessViolation, check_access
from .records import MemoryCategory, MemoryRecord, MemorySource, WritePolicy
from .repository import MemoryRepository
from .ranking import RankedHit, re_rank_results
from .security_guard import (
    MemorySecurityGuard,
    MemorySecurityViolation,
    NoopMemorySecurityGuard,
)
from .write_guards import authorize_explicit_write, infer_category

# Automatic sources can never write long-term memory directly (Boundary
# Repair v1): candidates must go through SuggestionService pending and be
# user-confirmed in the Memory Manager before any remember() succeeds.
AUTO_SOURCE_TRIGGERS = frozenset(
    {"companion_auto", "conversation_summary", "system_generated"}
)

logger = logging.getLogger(__name__)
from difflib import SequenceMatcher
from .m2 import MemoryCandidate, MemoryWritePolicy, WriteAction as M2Action, kind_from_category


class SemanticIndex(Protocol):
    """Narrow vector operations consumed by MemoryService."""

    def add(self, text: str, metadata: Mapping[str, Any] | None = None) -> str: ...

    def search(
        self, query: str, *, limit: int = 5, threshold: float = 0.0
    ) -> list[Hit]: ...

    def delete(self, vector_id: str) -> bool: ...


class MemorySynchronizationError(RuntimeError):
    """Raised when the semantic index cannot be synchronized with truth."""

    def __init__(self, message: str, record: MemoryRecord) -> None:
        super().__init__(message)
        self.record = record


class WriteOutcome(str, Enum):
    CREATED = "created"
    EXACT_DUPLICATE = "exact_duplicate"
    SEMANTIC_DUPLICATE = "semantic_duplicate"


@dataclass(frozen=True, slots=True)
class WriteResult:
    outcome: WriteOutcome
    record: MemoryRecord


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    missing_indexes: tuple[str, ...]
    orphan_indexes: tuple[str, ...]
    healthy: int
    stale_links: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryConsistencyReport:
    """Read-only divergence report between repository and semantic index.

    Identity contract: ``MemoryRecord.id`` is canonical. ``vector_id`` is
    derived index metadata and never decides whether a memory exists.
    """

    repository_records: int
    indexed_records: int
    missing_vectors: tuple[str, ...]
    orphan_vectors: tuple[str, ...]
    stale_vector_links: tuple[str, ...]
    healthy: int


class MemoryService:
    """The sole production write path for authoritative MemoryRecords."""

    def __init__(
        self,
        repository: MemoryRepository,
        adapter: SemanticIndex | Mem0Adapter,
        *,
        write_policy: WritePolicy | str = WritePolicy.EXPLICIT_ONLY,
        search_top_k: int = 5,
        search_threshold: float = 0.0,
        security_guard: MemorySecurityGuard | None = None,
        dedup_enabled: bool = True,
        dedup_similarity_threshold: float = 0.85,
        access_mode: MemoryAccessMode | str = MemoryAccessMode.SAFE_WRITE,
    ) -> None:
        self.repository = repository
        self.adapter = adapter
        self.write_policy = WritePolicy(write_policy)
        self.search_top_k = search_top_k
        self.search_threshold = search_threshold
        self.security_guard: MemorySecurityGuard = (
            security_guard if security_guard is not None else NoopMemorySecurityGuard()
        )
        self.dedup_enabled = dedup_enabled
        self.dedup_similarity_threshold = dedup_similarity_threshold
        # M3B.1: capability gate for every mutating entry point.  A READ_ONLY
        # handle refuses add/update/delete/lifecycle outright (loudly); a
        # SAFE_WRITE handle may remember but not delete/clear/migrate.
        self.access_mode = (
            access_mode
            if isinstance(access_mode, MemoryAccessMode)
            else MemoryAccessMode(str(access_mode))
        )
        # In-memory only (no schema): set when the derived index could not be
        # synchronized. Read models stay valid; reconcile() heals and clears it.
        self.index_dirty = False
        # M2A: unified write policy for dedup / conflict / supersede decisions.
        self.m2_policy = MemoryWritePolicy()

    @classmethod
    def local(
        cls,
        repository: MemoryRepository,
        storage_dir: str | None = None,
        *,
        user_id: str | None = None,
        write_policy: WritePolicy | str = WritePolicy.EXPLICIT_ONLY,
        search_top_k: int = 5,
        search_threshold: float = 0.0,
        security_guard: MemorySecurityGuard | None = None,
        dedup_enabled: bool = True,
        dedup_similarity_threshold: float = 0.85,
        access_mode: MemoryAccessMode | str = MemoryAccessMode.SAFE_WRITE,
    ) -> MemoryService:
        """Construct the standard local semantic adapter behind this service."""
        adapter = (
            Mem0Adapter(storage_dir)
            if user_id is None
            else Mem0Adapter(storage_dir, user_id=user_id)
        )
        return cls(
            repository,
            adapter,
            write_policy=write_policy,
            search_top_k=search_top_k,
            search_threshold=search_threshold,
            security_guard=security_guard,
            dedup_enabled=dedup_enabled,
            dedup_similarity_threshold=dedup_similarity_threshold,
            access_mode=access_mode,
        )

    def remember(
        self,
        user_input: str,
        *,
        category: MemoryCategory | str | None = None,
        trigger: str = "explicit-command",
        permission: WritePolicy | str | None = None,
        asserted_explicit: bool = False,
    ) -> MemoryRecord | None:
        """Persist an explicit request; returns the stored record (existing on dedup)."""
        result = self.remember_detailed(
            user_input,
            category=category,
            trigger=trigger,
            permission=permission,
            asserted_explicit=asserted_explicit,
        )
        return result.record if result is not None else None

    def remember_detailed(
        self,
        user_input: str,
        *,
        category: MemoryCategory | str | None = None,
        trigger: str = "explicit-command",
        permission: WritePolicy | str | None = None,
        asserted_explicit: bool = False,
    ) -> WriteResult | None:
        """Run the full write pipeline and report the outcome explicitly.

        Ordinary chat returns None. Otherwise the outcome is CREATED,
        EXACT_DUPLICATE, or SEMANTIC_DUPLICATE. Deduplication never mutates the
        authoritative content and never writes repository or Mem0.
        """
        requested_policy = self.write_policy if permission is None else WritePolicy(permission)
        # A call-site snapshot may further restrict a configured service, but
        # it must never override a globally disabled write policy.
        effective_policy = (
            WritePolicy.OFF
            if self.write_policy is WritePolicy.OFF
            else requested_policy
        )
        content = authorize_explicit_write(
            user_input,
            effective_policy,
            asserted_explicit=asserted_explicit,
        )
        if content is None:
            return None
        content = self._apply_security_guard(content)
        effective_category = (
            infer_category(content) if category is None else MemoryCategory(category)
        )

        # Companion Memory Boundary Repair: automatic sources must never
        # write long-term memory, no matter what the caller asserts.  These
        # triggers identify machine-originated candidates; the legitimate
        # route for them is SuggestionService pending -> user confirmation
        # (which re-enters here with a user-confirmed trigger).
        if trigger in AUTO_SOURCE_TRIGGERS:
            logger.info(
                "Boundary guard: refused automatic memory write "
                "(trigger=%s, asserted_explicit=%s)",
                trigger, asserted_explicit,
            )
            return None

        # M3B.1 access gate: remembering is SAFE_WRITE-class.  READ_ONLY
        # handles (retrieval, benchmark, export tooling) refuse loudly.
        check_access(self.access_mode, MemoryAccessMode.SAFE_WRITE,
                     "MemoryService.remember_detailed")

        # M2A: unified policy for dedup / conflict / supersede decisions.
        # Replaces the old ad-hoc exact+semantic dup checks.
        from memory.m2 import kind_from_category

        candidate = MemoryCandidate(
            content=content,
            kind=kind_from_category(
                effective_category.value if hasattr(effective_category, "value") else str(effective_category)
            ),
            source="explicit_user",
            explicit=True,
            confidence=1.0,
        )
        all_records = self.repository.list()
        decision = self.m2_policy.evaluate(candidate, all_records)

        if decision.action is M2Action.IGNORE_DUPLICATE:
            existing_id = decision.target_record_ids[0] if decision.target_record_ids else ""
            existing = (
                next(
                    (r for r in all_records if getattr(r, "id", "") == existing_id),
                    None,
                )
                if existing_id
                else None
            )
            if existing is not None:
                return WriteResult(WriteOutcome.EXACT_DUPLICATE, existing)

        if decision.action is M2Action.DO_NOT_PERSIST:
            return None

        # M2B.1 regression guard: the M2A policy is text-only and cannot see
        # the semantic index.  For plain CREATE/KEEP_BOTH decisions the old
        # adapter-backed semantic dedup still applies (same category + score
        # >= dedup threshold → suppressed).  SUPERSEDE keeps priority: a
        # genuine revision must win over dedup so conflicting info is never
        # silently dropped.
        if decision.action is not M2Action.SUPERSEDE:
            semantic_dup = self._find_semantic_duplicate(content, effective_category)
            if semantic_dup is not None:
                return WriteResult(WriteOutcome.SEMANTIC_DUPLICATE, semantic_dup)

        record = MemoryRecord.create(
            category=effective_category,
            content=content,
            source=MemorySource.EXPLICIT,
            trigger=trigger,
            permission=effective_policy,
        )
        self.repository.add(record, access_mode=self.access_mode)
        try:
            vector_id = self.adapter.add(content, {"record_id": record.id})
        except Exception as exc:
            self.index_dirty = True
            raise MemorySynchronizationError(
                "memory is authoritative but semantic indexing failed", record
            ) from exc
        try:
            updated = self.repository.update(record.id, {"vector_id": vector_id},
                                             access_mode=self.access_mode)
        except Exception as exc:
            raise MemorySynchronizationError(
                "semantic index exists but vector ID could not be recorded", record
            ) from exc

        # SUPERSEDE: mark old records (after new record is safely persisted).
        # M2C.1 atomicity: if supersede fails → compensating rollback
        # (delete newly created record) to avoid two mutually exclusive active
        # facts coexisting.
        if decision.action is M2Action.SUPERSEDE:
            supersede_failed = False
            for old_id in decision.target_record_ids:
                try:
                    self.apply_supersede(
                        old_id, record.id,
                        reason=getattr(decision, "supersede_reason", "confirmed_conflict"),
                    )
                except Exception as exc:  # noqa: BLE001
                    supersede_failed = True
                    logger.warning("supersede failed for %s: %s", old_id, exc)
            if supersede_failed:
                # Compensating rollback: remove the new record so we don't
                # leave two mutually exclusive active facts.  Explicit
                # CONFIRMED_WRITE here is the M2C.1 atomicity guarantee of the
                # SAFE_WRITE creation flow itself — it only ever removes the
                # record this same call just created.
                try:
                    self.repository.delete(
                        record.id, access_mode=MemoryAccessMode.CONFIRMED_WRITE,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("rollback delete also failed")
                return WriteResult(
                    WriteOutcome.EXACT_DUPLICATE,
                    self.repository.get(decision.target_record_ids[0])
                    or updated,
                )

        return WriteResult(WriteOutcome.CREATED, updated)

    @staticmethod
    def _normalize_for_compare(content: str) -> str:
        """Comparison-only normalization; never mutates stored content."""
        return "".join(ch for ch in content.casefold() if ch.isalnum())

    def _find_exact_duplicate(self, content: str) -> MemoryRecord | None:
        normalized = self._normalize_for_compare(content)
        for record in self.repository.list():
            if self._normalize_for_compare(record.content) == normalized:
                return record
        return None

    def _find_semantic_duplicate(
        self, content: str, category: MemoryCategory
    ) -> MemoryRecord | None:
        if not self.dedup_enabled:
            return None
        if not self.repository.all_ids():
            return None
        try:
            hits = self.adapter.search(content, limit=self.search_top_k)
        except Exception:
            return None
        for hit in hits:
            if hit.score < self.dedup_similarity_threshold:
                continue
            record_id = hit.metadata.get("record_id")
            if not isinstance(record_id, str) or not record_id.strip():
                continue
            record = self.repository.get(record_id)
            if record is not None and record.category is category:
                return record
        return None

    def _apply_security_guard(self, content: str) -> str:
        """Run the optional second-layer guard after Firefly's write guards."""
        decision = self.security_guard.guard(content)
        if decision.action == "block":
            raise MemorySecurityViolation(
                decision.reason or "memory write blocked by security guard"
            )
        if decision.action == "redact" and decision.content is not None:
            redacted = decision.content.strip()
            if redacted:
                return redacted
        return content

    def import_migrated(
        self,
        content: str,
        *,
        category: MemoryCategory | str,
        trigger: str,
    ) -> MemoryRecord:
        """Persist one reviewed migration item (M3B.1: CONFIRMED_WRITE-class).

        Unlike :meth:`remember`, this trusted path does not pretend historical
        content was a new explicit command. Callers must supply an auditable
        ``migration:<run_id>`` trigger.
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError("migration content must be a non-empty string")
        if not isinstance(trigger, str) or not trigger.startswith("migration:"):
            raise ValueError("migration trigger must start with 'migration:'")
        check_access(self.access_mode, MemoryAccessMode.CONFIRMED_WRITE,
                     "MemoryService.import_migrated")
        normalized_content = content.strip()
        record = MemoryRecord.create(
            category=category,
            content=normalized_content,
            source=MemorySource.MIGRATED,
            trigger=trigger,
            permission=WritePolicy.AUTO,
        )
        self.repository.add(record, access_mode=self.access_mode)
        try:
            vector_id = self.adapter.add(
                normalized_content, {"record_id": record.id}
            )
        except Exception as exc:
            self.index_dirty = True
            raise MemorySynchronizationError(
                "migrated memory is authoritative but semantic indexing failed",
                record,
            ) from exc
        try:
            return self.repository.update(record.id, {"vector_id": vector_id})
        except Exception as exc:
            raise MemorySynchronizationError(
                "migrated semantic index exists but vector ID could not be recorded",
                record,
            ) from exc

    def search(
        self, query: str, *, limit: int | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryRecord]:
        """Resolve semantic hits back to authoritative MemoryRecords with ranking.

        Superseded records are excluded by default (the active-only filter).
        Over-fetches from the semantic adapter so that top-k truncation cannot
        hide an active canonical behind a wall of superseded duplicates.

        The pipeline is:
        1. Mem0 semantic search (over-fetched for superseded headroom)
        2. Resolve hits to authoritative MemoryRecords
        3. Deterministic re-ranking (category weight x importance x temporal decay)
        4. Client-side threshold filter on final_score
        5. Superseded filter (unless include_superseded=True)
        6. Deduplicate by record id, return top-N
        """
        effective_limit = self.search_top_k if limit is None else limit
        ranked = self._search_ranked(
            query, limit=effective_limit, include_superseded=include_superseded
        )
        return [rh.record for rh in ranked][:effective_limit]

    def _search_ranked(
        self, query: str, *, limit: int | None = None,
        include_superseded: bool = False,
    ) -> list[RankedHit]:
        """Shared search pipeline returning RankedHits (records + scores).

        ``search`` projects this down to bare records; M3B retrieval uses the
        raw ``semantic_score`` per hit so prompt-side ranking does not have to
        approximate relevance from result order.
        """
        effective_limit = self.search_top_k if limit is None else limit
        # Over-fetch adaptively: start with 2× headroom and increase until
        # enough active results or all records exhausted. A fixed ratio cannot
        # guarantee that the active canonical surfaces when a cluster has
        # many more superseded duplicates than active records.
        total_records = len(self.repository.list())
        fetch_limit = max(effective_limit * 2, min(total_records, 15))
        hits = self.adapter.search(
            query, limit=fetch_limit, threshold=self.search_threshold
        )
        by_vector_id = {
            record.vector_id: record.id
            for record in self.repository.list()
            if record.vector_id is not None
        }

        # Resolve hits to MemoryRecords and collect semantic scores
        semantic_scores: dict[str, float] = {}
        resolved: list[MemoryRecord] = []
        seen: set[str] = set()
        for hit in hits:
            record_id = hit.metadata.get("record_id")
            if not isinstance(record_id, str) or not record_id.strip():
                record_id = by_vector_id.get(hit.vector_id)
            if not isinstance(record_id, str) or record_id in seen:
                continue
            record = self.repository.get(record_id)
            if record is None:
                continue
            semantic_scores[record.id] = hit.score
            resolved.append(record)
            seen.add(record.id)

        # Deterministic re-ranking
        ranked = re_rank_results(resolved, semantic_scores)

        # Client-side threshold on final_score
        filtered = [
            rh for rh in ranked if rh.final_score >= self.search_threshold
        ]

        # Superseded filter BEFORE top-N cut. If insufficient active results
        # and there are more indexed records, escalate to full fetch so that
        # superseded duplicates cannot hide the active canonical.
        if not include_superseded:
            filtered = [
                rh for rh in filtered
                if getattr(rh.record, "lifecycle_status", "active") != "superseded"
            ]
            if len(filtered) < effective_limit and fetch_limit < total_records:
                # Adaptive second pass: re-fetch ALL indexed records
                all_hits = self.adapter.search(
                    query, limit=total_records, threshold=self.search_threshold
                )
                all_resolved: list[MemoryRecord] = []
                all_scores: dict[str, float] = {}
                all_seen: set[str] = set()
                for hit in all_hits:
                    rid = hit.metadata.get("record_id")
                    if not isinstance(rid, str) or not rid.strip():
                        rid = by_vector_id.get(hit.vector_id)
                    if not isinstance(rid, str) or rid in all_seen:
                        continue
                    rec = self.repository.get(rid)
                    if rec is None or rec.lifecycle_status == "superseded":
                        continue
                    all_scores[rec.id] = hit.score
                    all_resolved.append(rec)
                    all_seen.add(rec.id)
                all_ranked = re_rank_results(all_resolved, all_scores)
                filtered = [
                    rh for rh in all_ranked
                    if rh.final_score >= self.search_threshold
                ]

        return filtered

    def delete(self, record_id: str) -> bool:
        """Delete semantic index entries first, then the authoritative record.

        M2C.1 chain repair: if any record's ``superseded_by`` points at the
        record being deleted, the chain is rewired to the deleted record's own
        ``superseded_by`` target (skipping the deleted record). This prevents
        dangling superseded_by links.

        M3B.1: CONFIRMED_WRITE-class — READ_ONLY / SAFE_WRITE handles refuse.
        """
        check_access(self.access_mode, MemoryAccessMode.CONFIRMED_WRITE,
                     "MemoryService.delete")
        record = next(
            (item for item in self.repository.list() if item.id == record_id),
            None,
        )
        if record is None:
            return False

        # M2C.1: repair superseded_by chain before deletion
        for r in self.repository.list():
            if r.superseded_by == record_id and r.id != record_id:
                repaired_target = record.superseded_by
                if repaired_target and repaired_target != r.id:
                    self.repository.update(
                        r.id, {"superseded_by": repaired_target},
                        access_mode=self.access_mode,
                    )
                elif not repaired_target:
                    self.repository.update(
                        r.id,
                        {"superseded_by": None, "lifecycle_status": "active"},
                        access_mode=self.access_mode,
                    )

        candidate_ids: list[str] = []
        if record.vector_id is not None:
            candidate_ids.append(record.vector_id)
        for hit in self._index_entries():
            hit_record_id = hit.metadata.get("record_id")
            if hit_record_id == record.id and hit.vector_id not in candidate_ids:
                candidate_ids.append(hit.vector_id)

        index_failure: Exception | None = None
        for vector_id in candidate_ids:
            try:
                self.adapter.delete(vector_id)
            except Exception as exc:
                index_failure = exc
        if index_failure is not None:
            self.index_dirty = True
            raise MemorySynchronizationError(
                "semantic index deletion failed; authoritative record retained",
                record,
            ) from index_failure
        return self.repository.delete(record_id, access_mode=self.access_mode)

    def clear_all(self) -> int:
        """Clear authoritative records first, then the derived semantic index.

        M3B.1: CONFIRMED_WRITE-class.

        SOURCE OF TRUTH FIRST: the repository is cleared before the index, so
        a failed index clear can never resurrect or fake data. A failed index
        clear marks the service ``index_dirty``; the orphan vectors remain
        discoverable via :meth:`consistency_report` and removable via
        :meth:`reconcile`.
        """
        check_access(self.access_mode, MemoryAccessMode.CONFIRMED_WRITE,
                     "MemoryService.clear_all")
        count = self.repository.clear(access_mode=self.access_mode)
        try:
            self.adapter.clear()
        except Exception as exc:
            self.index_dirty = True
            raise MemorySynchronizationError(
                "memory repository cleared but semantic index cleanup failed; "
                "orphan vectors remain until reconcile()",
                MemoryRecord.create(
                    category=MemoryCategory.USER_FACT,
                    content="<clear-all>",
                    source=MemorySource.EXPLICIT,
                    trigger="clear-all",
                    permission=WritePolicy.OFF,
                ),
            ) from exc
        return count

    def consistency_report(self) -> MemoryConsistencyReport:
        """Read-only divergence report (no content leaves the service)."""
        records = self.repository.list()
        entries = self._index_entries()
        indexed_vector_ids = {hit.vector_id for hit in entries}

        entry_by_record: dict[str, str] = {}
        for hit in entries:
            hit_record_id = hit.metadata.get("record_id")
            if isinstance(hit_record_id, str) and hit_record_id:
                entry_by_record.setdefault(hit_record_id, hit.vector_id)

        missing: list[str] = []
        stale: list[str] = []
        healthy = 0
        for record in records:
            link_ok = (
                record.vector_id is not None
                and record.vector_id in indexed_vector_ids
            )
            retrievable = record.id in entry_by_record or link_ok
            if retrievable and link_ok:
                healthy += 1
            elif retrievable:
                # Indexed under the record_id payload, but the stored link is
                # stale/missing — retrievable, yet the link needs repair.
                stale.append(record.id)
            elif record.vector_id is None:
                missing.append(record.id)
            else:
                stale.append(record.id)   # dead link, indexed nowhere

        valid_ids = {record.id for record in records}
        claimed_vector_ids = {record.vector_id for record in records if record.vector_id}
        orphans = [
            hit.vector_id
            for hit in entries
            if hit.metadata.get("record_id") not in valid_ids
            and hit.vector_id not in claimed_vector_ids
        ]
        return MemoryConsistencyReport(
            repository_records=len(records),
            indexed_records=len(entries),
            missing_vectors=tuple(missing),
            orphan_vectors=tuple(orphans),
            stale_vector_links=tuple(stale),
            healthy=healthy,
        )

    def edit_memory(
        self, record_id: str, new_content: str, *,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Edit an existing memory: old → superseded, new → active.

        M2C.1: the mutation target comes from the user's explicit selection
        (record_id), NOT from semantic policy guessing. The policy still
        checks for duplicates and validates the new content.

        M3B.1: CONFIRMED_WRITE-class.

        Returns ``{"action": ..., "record": ..., "message": ...}``.
        """
        check_access(self.access_mode, MemoryAccessMode.CONFIRMED_WRITE,
                     "MemoryService.edit_memory")
        old = self.repository.get(record_id)
        if old is None:
            return {"action": "error", "message": "记录不存在"}
        effective_category = category or old.category.value if hasattr(old.category, "value") else str(old.category)

        # Policy check: exact duplicate against OTHER active records
        candidate = MemoryCandidate(
            content=new_content,
            kind=kind_from_category(effective_category),
            source="manual_edit",
            explicit=True,
            confidence=1.0,
        )
        other_active = [
            r for r in self.repository.list()
            if r.id != record_id and r.lifecycle_status != "superseded"
        ]
        decision = self.m2_policy.evaluate(candidate, other_active)

        if decision.action is WriteAction.IGNORE_DUPLICATE:
            return {
                "action": "duplicate",
                "existing_id": decision.target_record_ids[0] if decision.target_record_ids else None,
                "message": "这条记忆已经存在。",
            }

        # Create new revision (never in-place overwrite)
        record = MemoryRecord.create(
            category=old.category,
            content=new_content,
            source=MemorySource.EXPLICIT,
            trigger="manual_edit",
            permission=old.permission,
        )
        self.repository.add(record, access_mode=self.access_mode)
        try:
            vector_id = self.adapter.add(new_content, {"record_id": record.id})
            self.repository.update(
                record.id, {"vector_id": vector_id},
                access_mode=self.access_mode,
            )
        except Exception:
            self.index_dirty = True

        # Mark old as superseded (user explicitly selected this record)
        self.apply_supersede(record_id, record.id, reason="manual_edit")
        return {"action": "created", "record": record, "old_id": record_id}

    def retrieve_for_prompt(
        self, query: str, *,
        max_injected: int | None = None,
        min_relevance: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> list[MemoryRecord]:
        """M3B context-aware retrieval: semantic search → M3B scoring →
        threshold → recall gate → diversity → top-N for prompt injection.

        Deterministic (no LLM calls) and read-only (no repository mutation).
        Returns [] when no memory is relevant enough — this is a valid
        result, not an error.

        ``context`` (optional, M3B.3) may carry conversation_history /
        mode / intent for the recall gate; the default rules use only the
        query and candidates.
        """
        from memory.m3b import (
            MAX_INJECTED_MEMORIES, MIN_RELEVANCE_SCORE,
            MemoryRetrievalCandidate, score_candidates,
            _suppress_near_duplicates,
        )
        from memory.recall_gate import (  # noqa: F401
            GateDecision, RecallDecision, MemoryRecallGate,
            _EXPLICIT_MARKERS, _contains_any,
        )

        k = max_injected if max_injected is not None else MAX_INJECTED_MEMORIES
        threshold = min_relevance if min_relevance is not None else MIN_RELEVANCE_SCORE

        # v1.1 Companion Mode: 显式回忆型问句 ("最近我在做什么/还记得…吗") 的
        # 字面语义分常落在 0.32-0.45 (低于常规阈值但高于无关噪声 ~0.36 底线),
        # 此时放宽检索阈值, 交由 recall gate 的显式回忆规则放行。
        lowered_for_recall = False
        if min_relevance is None and _contains_any(str(query or ""), _EXPLICIT_MARKERS):
            threshold = min(threshold, 0.32)
            lowered_for_recall = True

        ranked = self._search_ranked(
            query, limit=max(k * 3, 10), include_superseded=False
        )
        if not ranked:
            return []

        # Build candidates from the REAL semantic scores (not rank proxies)
        candidates: list[MemoryRetrievalCandidate] = []
        for rh in ranked:
            record = rh.record
            cat = record.category
            cat_val = cat.value if isinstance(cat, Enum) else str(cat)
            src = record.source
            src_val = src.value if isinstance(src, Enum) else str(src)
            candidates.append(MemoryRetrievalCandidate(
                record_id=record.id,
                content=record.content,
                semantic_score=rh.semantic_score,
                kind=kind_from_category(cat_val).value,
                source=str(src_val),
                created_ts=record.created_ts,
                updated_ts=record.updated_ts,
                lifecycle_status=getattr(record, "lifecycle_status", "active"),
            ))

        scored = score_candidates(candidates)
        scored.sort(key=lambda s: s.final_score, reverse=True)

        # Threshold gate — M3B.2 correction: the gate applies to the SEMANTIC
        # signal it was calibrated on (unrelated ≤ ~0.36, related ≥ ~0.46
        # cosine), not to the context-multiplied score.  Multiplying first let
        # a +17% context bonus push pure-chitchat matches (sem ≈ 0.39) over
        # the line — false injections on 闲聊 queries in the real-embedder
        # evaluation.  Context bonuses RANK candidates; they never lower the
        # relevance bar.
        above = [s for s in scored if s.semantic_score >= threshold]
        if not above:
            return []
        if lowered_for_recall:
            print(f"[recall-intent] {len(above)} candidates at lowered threshold {threshold:.2f}")

        # M3B.3 Recall Gate — a second, orthogonal deterministic question:
        # not "is this candidate relevant" (threshold above) but "does this
        # QUERY warrant remembering" (explicit reference vs smalltalk vs
        # knowledge Q&A vs ordinary).  Inserted between the threshold and
        # diversity; repository / ranking / embedding / threshold untouched.
        above_ids = {s.record_id for s in above}
        gate_candidates = [c for c in candidates if c.record_id in above_ids]
        gate = MemoryRecallGate()
        gate_decision = gate.should_inject(query, gate_candidates, context=dict(context or {}))
        if lowered_for_recall and gate_decision.decision.value == "block":
            # 显式回忆意图 + 候选已过放宽阈值 → 覆盖闸门的默认拦截
            gate_decision = RecallDecision(
                GateDecision.ALLOW,
                "explicit_recall_intent + lowered_threshold_candidates",
                tuple(gate_candidates),
            )
        allowed_ids = set(gate_decision.allowed_ids)
        above = [s for s in above if s.record_id in allowed_ids]
        if not above:
            return []

        # Diversity check (suppress near-identical), then cap at budget
        contents = {c.record_id: c.content for c in candidates}
        selected = _suppress_near_duplicates(above, contents)[:k]

        by_id = {rh.record.id: rh.record for rh in ranked}
        return [by_id[s.record_id] for s in selected if s.record_id in by_id]

    def _index_entries(self) -> list[Hit]:
        """Best-effort full index listing (empty when unsupported/unavailable)."""
        list_entries = getattr(self.adapter, "list_entries", None)
        if not callable(list_entries):
            return []
        try:
            return list(list_entries())
        except Exception:
            return []

    def list(
        self,
        *,
        category: MemoryCategory | str | None = None,
        source: MemorySource | str | None = None,
        before_ts: int | None = None,
    ) -> list[MemoryRecord]:
        return self.repository.list(
            category=category,
            source=source,
            before_ts=before_ts,
        )

    def reconcile(self, *, dry_run: bool = True) -> ReconcileResult:
        """Repair divergence with the repository as the sole input.

        ``dry_run=True`` only reports. ``dry_run=False`` additionally:
        1. re-indexes records whose content is missing from the index
           (``vector_id`` None **or** pointing at nothing — stale links);
        2. repairs broken ``vector_id`` links when the content IS indexed
           under the record's ``record_id`` payload (repository update only);
        3. deletes orphan index entries.

        Reconcile never modifies MemoryRecord content and never dedupes,
        merges, or resolves conflicts.

        M3B.1: the repair pass (``dry_run=False``) is CONFIRMED_WRITE-class;
        the report pass stays available to READ_ONLY handles.
        """
        if not dry_run:
            check_access(self.access_mode, MemoryAccessMode.CONFIRMED_WRITE,
                         "MemoryService.reconcile(write)")
        records = self.repository.list()
        entries = self._index_entries()
        indexed_vector_ids = {hit.vector_id for hit in entries}

        entry_by_record: dict[str, str] = {}
        for hit in entries:
            hit_record_id = hit.metadata.get("record_id")
            if isinstance(hit_record_id, str) and hit_record_id:
                entry_by_record.setdefault(hit_record_id, hit.vector_id)

        missing: list[MemoryRecord] = []
        broken_links: list[tuple[MemoryRecord, str]] = []
        for record in records:
            if record.id in entry_by_record:
                if record.vector_id != entry_by_record[record.id]:
                    broken_links.append((record, entry_by_record[record.id]))
                continue
            if record.vector_id is not None and (
                record.vector_id in indexed_vector_ids
            ):
                continue  # healthy
            missing.append(record)

        orphan_ids: list[str] = []
        valid_ids = {record.id for record in records}
        claimed_vector_ids = {
            record.vector_id for record in records if record.vector_id
        }
        for hit in entries:
            hit_record_id = hit.metadata.get("record_id")
            if (
                not isinstance(hit_record_id, str) or hit_record_id not in valid_ids
            ) and hit.vector_id not in claimed_vector_ids:
                orphan_ids.append(hit.vector_id)

        if not dry_run:
            for record in missing:
                vector_id = self.adapter.add(
                    record.content, {"record_id": record.id}
                )
                self.repository.update(
                    record.id, {"vector_id": vector_id},
                    access_mode=self.access_mode,
                )
            for record, vector_id in broken_links:
                self.repository.update(
                    record.id, {"vector_id": vector_id},
                    access_mode=self.access_mode,
                )
            for vector_id in orphan_ids:
                self.adapter.delete(vector_id)
            self.index_dirty = False
            records = self.repository.list()

        healthy = sum(
            1 for record in records
            if record.id in entry_by_record
            or (
                record.vector_id is not None
                and record.vector_id in indexed_vector_ids
            )
        )
        stale_ids = tuple({record.id for record, _ in broken_links})
        return ReconcileResult(
            missing_indexes=tuple(record.id for record in missing),
            orphan_indexes=tuple(orphan_ids),
            healthy=healthy,
            stale_links=stale_ids,
        )

    def apply_supersede(self, old_id: str, new_id: str, *,
                       reason: str = "confirmed_conflict") -> None:
        """Mark ``old_id`` as superseded by ``new_id`` (never deletes).

        M2C: cycle-protected. If new_id's supersede chain already reaches
        old_id, superseding would create a cycle and is rejected.
        """
        if old_id == new_id:
            raise ValueError("cannot supersede a record by itself")
        # M3B.1: lifecycle change — READ_ONLY refuses; SAFE_WRITE+ is allowed
        # because the creation flow (M2A conflict resolution) supersedes as
        # part of a normal remember.
        check_access(self.access_mode, MemoryAccessMode.SAFE_WRITE,
                     "MemoryService.apply_supersede")
        # Cycle check: walk the supersede chain from new_id
        visited: set[str] = set()
        current: str | None = new_id
        while current is not None and current not in visited:
            if current == old_id:
                raise ValueError(
                    f"supersede would create a cycle: {old_id} ← {new_id}"
                )
            visited.add(current)
            rec = self.repository.get(current)
            if rec is None:
                break
            current = rec.superseded_by

        self.repository.update(old_id, {
            "lifecycle_status": "superseded",
            "superseded_by": new_id,
            "supersede_reason": reason,
        }, access_mode=self.access_mode)

    def resolve_active_successor(self, record_id: str) -> MemoryRecord | None:
        """Follow the superseded_by chain to the final active successor.

        Cycle-protected: raises on A→B→A.
        """
        visited: set[str] = set()
        current_id: str | None = record_id
        while current_id is not None:
            if current_id in visited:
                raise ValueError(f"supersede cycle detected at {current_id}")
            visited.add(current_id)
            record = self.repository.get(current_id)
            if record is None:
                return None
            if record.lifecycle_status != "superseded" or not record.superseded_by:
                return record
            current_id = record.superseded_by


__all__ = [
    "MemoryConsistencyReport",
    "MemoryService",
    "MemorySecurityViolation",
    "MemorySynchronizationError",
    "ReconcileResult",
    "SemanticIndex",
    "WriteOutcome",
    "WriteResult",
]
