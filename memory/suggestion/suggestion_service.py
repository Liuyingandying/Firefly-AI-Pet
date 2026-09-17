"""Pending-suggestion service with user-confirmed accept/reject.

Holds candidate memories until the user confirms or discards them. Accepting
writes through the existing ``MemoryService.remember()`` interface (never a
direct repository write); rejecting simply drops the candidate.

P5A-1 upgrades:
- LLM-based candidate extraction via ``MemoryCandidateExtractor``.
- Privacy/security filtering on all candidates before they become pending.
- Deduplication against existing pending suggestions and existing MemoryRecords.
- Explicit "记住" request handling → high-confidence pending suggestion.
- Observability logging for extraction lifecycle events.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Sequence

from memory.records import MemoryCategory, MemoryRecord
from memory.security_guard import (
    GuardDecision,
    MemorySecurityGuard,
    MemorySecurityViolation,
    NoopMemorySecurityGuard,
)
from memory.write_guards import detect_red_line
from memory.suggestion.candidate_extractor import (
    ExtractionResult,
    MemoryCandidateExtractor,
)
from memory.suggestion.memory_candidate_detector import (
    MemoryCandidateDetector,
    MemorySuggestion,
)
from memory.suggestion.semantic_dedup import SemanticDedup
from memory.suggestion_store import SuggestionStore

logger = logging.getLogger(__name__)


class SuggestionService:
    """Accumulate and resolve candidate memories for one conversation.

    The service supports two entry points for creating pending suggestions:

    1. **LLM-based extraction** (``extract_candidates``):
       Runs the LLM extractor on a completed chat turn, filters candidates
       through privacy/security guards, deduplicates, and queues the rest
       as pending suggestions.

    2. **Rule-based detection** (``detect``):
       The existing regex-based detector for simple signals.

    3. **Explicit remember** (``handle_explicit_remember``):
       Handles user requests like "记住……" by creating a high-confidence
       pending suggestion through the normal guard pipeline.
    """

    def __init__(
        self,
        memory_service: Any,
        detector: MemoryCandidateDetector | None = None,
        extractor: MemoryCandidateExtractor | None = None,
        *,
        enabled: bool = True,
        auto_extract_enabled: bool = False,
        max_candidates_per_turn: int = 3,
        security_guard: MemorySecurityGuard | None = None,
        semantic_dedup_enabled: bool = True,
        semantic_dedup_threshold: float = 0.85,
        # P5A-3: explicit auto-approve settings
        explicit_auto_approve_enabled: bool = False,
        explicit_auto_approve_min_confidence: float = 0.95,
        explicit_auto_approve_allowed_categories: Sequence[str] | None = None,
        auto_write_enabled: bool = False,
        auto_write_min_chars: int = 8,
        auto_write_allowed_categories: Sequence[str] | None = None,
        store: "SuggestionStore | None" = None,
    ) -> None:
        self.memory_service = memory_service
        self.detector = detector or MemoryCandidateDetector()
        self.extractor = extractor
        self.enabled = enabled
        self.auto_extract_enabled = auto_extract_enabled
        self.max_candidates_per_turn = max_candidates_per_turn
        self.security_guard: MemorySecurityGuard = (
            security_guard if security_guard is not None else NoopMemorySecurityGuard()
        )
        self.semantic_dedup_enabled = semantic_dedup_enabled
        self.semantic_dedup = (
            SemanticDedup(threshold=semantic_dedup_threshold)
            if semantic_dedup_enabled
            else None
        )
        self._pending: list[MemorySuggestion] = []
        # suggestion id -> "accepted" | "rejected" (v1 observability; pending
        # suggestions themselves remain in-memory by design)
        self._decisions: dict[str, str] = {}
        # M3B.7: persistent suggestion store.  When attached, pending
        # candidates survive restarts; "explicit" is never a valid store
        # source (automatic candidates keep companion_auto/summary origins).
        self.store = store
        if self.store is not None:
            self._flush_ram_pending_to_store()

        # P5A-3: explicit auto-approve config
        self.explicit_auto_approve_enabled = explicit_auto_approve_enabled
        self.explicit_auto_approve_min_confidence = explicit_auto_approve_min_confidence
        self.explicit_auto_approve_allowed_categories: set[str] = (
            set(explicit_auto_approve_allowed_categories)
            if explicit_auto_approve_allowed_categories is not None
            else {MemoryCategory.PREFERENCE.value, MemoryCategory.PROJECT.value}
        )
        self.auto_write_enabled = auto_write_enabled
        self.auto_write_min_chars = auto_write_min_chars
        self.auto_write_allowed_categories: set[str] = set(
            auto_write_allowed_categories
            if auto_write_allowed_categories is not None
            else [MemoryCategory.PROJECT.value, MemoryCategory.PREFERENCE.value,
                  MemoryCategory.SHARED_EXPERIENCE.value, MemoryCategory.USER_FACT.value]
        )

    # ------------------------------------------------------------------ extract
    def extract_candidates(
        self,
        user_message: str,
        assistant_reply: str,
        context: Sequence[dict[str, str]] | None = None,
    ) -> ExtractionResult:
        """Run LLM extraction on a completed chat turn and queue valid candidates.

        Pipeline:
        1. Call the LLM extractor (safe no-op on failure).
        2. Filter candidates through privacy/security guards.
        3. Deduplicate against existing pending suggestions and MemoryRecords.
        4. Queue remaining candidates as pending suggestions.

        Returns the raw ExtractionResult from the extractor.
        """
        if not self.auto_extract_enabled:
            return ExtractionResult(
                candidates=[],
                status="zero_candidates",
            )

        if self.extractor is None:
            logger.debug("SuggestionService: no extractor configured")
            return ExtractionResult(
                candidates=[],
                status="provider_unavailable",
            )

        result = self.extractor.extract(user_message, assistant_reply, context)

        if not result.candidates:
            logger.debug(
                "Extraction %s: %d candidates", result.status, len(result.candidates)
            )
            return result

        # Privacy/security filtering
        filtered: list[MemorySuggestion] = []
        for candidate in result.candidates:
            if self._is_privacy_violation(candidate.content):
                logger.debug(
                    "Candidate rejected by privacy guard: %s", candidate.content[:50]
                )
                continue
            if self._security_guard_candidate(candidate) is None:
                logger.debug(
                    "Candidate rejected by security guard: %s", candidate.content[:50]
                )
                continue
            filtered.append(candidate)

        # Deduplication
        deduped = self._dedup_candidates(filtered)

        # Enforce max_candidates_per_turn cap
        capped = deduped[: self.max_candidates_per_turn]

        # Boundary repair: extracted candidates are CANDIDATES ONLY.  They
        # enter the pending list for user confirmation (MemoryPanel
        # 待确认 tab) and never write a MemoryRecord themselves — the
        # previous direct-write branch (remember/asserted_explicit) is gone.
        if capped:
            promoted, normal = [], []
            for cand in capped:
                tagged = replace(
                    cand,
                    source="companion_auto",
                    status="pending",
                    confidence=min(
                        1.0, cand.confidence + (0.05 if self.auto_write_enabled else 0.0)
                    ),
                )
                # companion gate still matters: gate-passing candidates are
                # surfaced at the TOP of the pending list (continuity UX);
                # promotion never writes.
                (
                    promoted if self._passes_companion_gate(tagged) else normal
                ).append(tagged)
            capped = promoted + normal

        if capped:
            if self.store is not None:
                # M3B.7: candidates go to the persistent SuggestionStore
                # (companion origin).  They never reach MemoryRepository
                # directly - user confirmation via 待确认 tab is the only
                # route into long-term memory.
                self._persist_pending(capped)
            else:
                self._pending.extend(capped)
            logger.info(
                "SuggestionService: created %d pending suggestions (from %d candidates, capped at %d)",
                len(capped),
                len(result.candidates),
                self.max_candidates_per_turn,
            )
        else:
            logger.debug("All candidates suppressed by dedup or max cap")

        return ExtractionResult(
            candidates=capped,
            status=result.status,
        )

    # --------------------------------------------------------- rule-based detect
    def detect(self, user_message: str) -> list[MemorySuggestion]:
        """Detect suggestions from a message and queue them as pending."""
        if not self.enabled:
            return []
        suggestions = self.detector.detect(user_message)
        if self.store is not None and suggestions:
            self._flush_ram_pending_to_store()
            for suggestion in suggestions:
                try:
                    self.store.add(
                        content=suggestion.content,
                        source="user_created",
                        confidence=suggestion.confidence,
                        status="pending",
                        metadata={
                            "category": getattr(
                                getattr(suggestion, "category", None),
                                "value", "project"
                            ),
                            "reason": suggestion.reason,
                            "evidence": list(suggestion.evidence),
                        },
                        created_at=suggestion.created_at,
                        suggestion_id=suggestion.id,
                    )
                except Exception:  # noqa: BLE001 - duplicates skipped
                    continue
            return suggestions
        self._pending.extend(suggestions)
        return suggestions

    # ------------------------------------------------------- explicit remember
    def handle_explicit_remember(
        self, user_message: str, confidence: float = 0.95
    ) -> list[MemorySuggestion]:
        """Handle an explicit "记住" request by creating a high-confidence suggestion.

        The content goes through the same privacy and dedup pipeline as
        LLM-extracted candidates. Never writes directly to MemoryRecord.

        Args:
            user_message: The user's explicit remember request text.
            confidence: Confidence score (default 0.95 for explicit requests).

        Returns:
            List of pending suggestions created (0 or 1).
        """
        if not user_message.strip():
            return []

        # Clean up the "记住" prefix to get the actual content
        content = self._strip_remember_prefix(user_message)
        if not content:
            return []

        # Use infer_category from write_guards for category assignment
        from memory.write_guards import infer_category

        category = infer_category(content)

        suggestion = MemorySuggestion(
            content=content,
            category=category,
            reason="explicit_remember",
            evidence=(user_message,),
            confidence=confidence,
        )

        # Privacy filter
        if self._is_privacy_violation(suggestion.content):
            logger.debug("Explicit remember rejected by privacy guard")
            return []

        # Security guard
        if self._security_guard_candidate(suggestion) is None:
            logger.debug("Explicit remember rejected by security guard")
            return []

        # Dedup
        existing_pending = {s.content for s in self._pending}
        if self._normalize_for_compare(suggestion.content) in {
            self._normalize_for_compare(s.content) for s in self._pending
        }:
            logger.debug("Explicit remember suppressed: duplicate pending")
            return []

        # Check against existing MemoryRecords
        if self._has_existing_memory(suggestion):
            logger.debug("Explicit remember suppressed: duplicate existing memory")
            return []

        # P5A-3: Auto-approve check (runs after all guards + dedup)
        auto_approved = self._try_auto_approve(suggestion)
        if auto_approved:
            return []  # Already written, no longer pending

        self._pending.append(suggestion)
        logger.info("SuggestionService: created explicit pending suggestion")
        return [suggestion]

    # ------------------------------------------------------------------ pending
    def _flush_ram_pending_to_store(self) -> None:
        """M3B.7 one-time migration: RAM pending -> SuggestionStore.

        Runs only when the store is empty; a non-empty store wins (the RAM
        list is stale by definition at construction time).
        """
        if self.store is None or not self._pending:
            return
        if self.store.count() > 0:
            self._pending.clear()
            return
        for suggestion in self._pending:
            try:
                self.store.add(
                    content=suggestion.content,
                    source="user_created",
                    confidence=suggestion.confidence,
                    status="pending",
                    metadata={
                        "category": getattr(
                            getattr(suggestion, "category", None), "value", "project"
                        ),
                        "reason": suggestion.reason,
                        "evidence": list(suggestion.evidence),
                    },
                    created_at=suggestion.created_at,
                    suggestion_id=suggestion.id,
                )
            except Exception:  # noqa: BLE001 - duplicate/invalid entries skipped
                continue
        self._pending.clear()

    def _persist_pending(self, suggestions: list[MemorySuggestion]) -> None:
        """Persist extracted candidates.  With a store attached they become
        durable pending suggestions (never Memory); without one the service
        falls back to the legacy in-RAM pending list."""
        if self.store is None:
            self._pending.extend(suggestions)
            return
        existing_pending = {
            r["content"] for r in self.store.get_pending()
        }
        for suggestion in suggestions:
            if suggestion.content in existing_pending:
                continue  # 跨重启去重：相同 pending 内容不重复入库
            confidence = suggestion.confidence
            metadata = {
                "category": getattr(
                    getattr(suggestion, "category", None), "value", "project"
                ),
                "reason": suggestion.reason,
                "evidence": list(suggestion.evidence),
            }
            if self.auto_write_enabled and self._passes_companion_gate(suggestion):
                # Companion Mode 连续陪伴标记：过门候选置信度小幅提升、置前
                confidence = min(1.0, confidence + 0.05)
                metadata["companion_promoted"] = True
            try:
                self.store.add(
                    content=suggestion.content,
                    source="companion_auto",
                    confidence=confidence,
                    status="pending",
                    metadata=metadata,
                    created_at=suggestion.created_at,
                    suggestion_id=suggestion.id,
                )
            except Exception:  # noqa: BLE001 - duplicate/invalid skipped
                continue

    def _store_pending_suggestions(self) -> list[MemorySuggestion]:
        """Store-backed pending list, reconstructed as MemorySuggestion."""
        from dataclasses import replace as _replace

        out: list[MemorySuggestion] = []
        for record in self.store.get_pending():
            base = MemorySuggestion(
                content=record["content"],
                category=_category_or_project(record.get("metadata", {}).get("category")),
                reason=str(record.get("metadata", {}).get("reason", "companion gate")),
                evidence=tuple(
                    record.get("metadata", {}).get("evidence", [])
                ),
                confidence=record["confidence"],
                source=record["source"],
                status=record["status"],
                created_at=record["created_at"],
                id=record["id"],
            )
            out.append(_replace(base, confidence=base.confidence))
        return out

    def list_pending(self) -> list[MemorySuggestion]:
        if self.store is not None:
            return self._store_pending_suggestions()
        return list(self._pending)

    def accept(self, suggestion: MemorySuggestion) -> Any:
        """Write one suggestion via ``MemoryService.remember`` and drop it."""
        try:
            if self.store is not None:
                record = self.memory_service.remember(
                    suggestion.content,
                    category=suggestion.category.value,
                    trigger="suggestion_confirmed",
                    asserted_explicit=True,
                )
            else:
                record = self.memory_service.remember(
                    suggestion.content,
                    category=suggestion.category.value,
                    trigger=f"suggestion:{suggestion.reason}",
                    asserted_explicit=True,
                )
        except Exception:
            return None
        self._decisions[suggestion.id] = "accepted"
        if self.store is not None:
            self.store.update_status(suggestion.id, "accepted")
        if record is not None and suggestion in self._pending:
            self._pending.remove(suggestion)
        return record

    def _passes_companion_gate(self, suggestion: MemorySuggestion) -> bool:
        """v1.1 筛选门: 只放行 长期项目/稳定偏好/重要经历/长期状态。

        拒绝: 闲聊向类别 (relationship/emotion)、过短内容、疑问句。
        """
        if suggestion.category.value not in self.auto_write_allowed_categories:
            return False
        content = (suggestion.content or "").strip()
        if len(content) < self.auto_write_min_chars:
            return False
        if content.endswith(("？", "?")):
            return False
        return True

    def reject(self, suggestion: MemorySuggestion) -> None:
        self._decisions[suggestion.id] = "rejected"
        if self.store is not None:
            self.store.update_status(suggestion.id, "rejected")
        if suggestion in self._pending:
            self._pending.remove(suggestion)

    # --------------------------------------------------------- P5A-3 auto-approve
    def _try_auto_approve(self, suggestion: MemorySuggestion) -> bool:
        """Attempt to auto-approve an explicit remember suggestion.

        Auto-approve only runs when ALL conditions are met:
        1. Feature is enabled (explicit_auto_approve_enabled=True)
        2. Suggestion has explicit_remember reason
        3. Confidence >= explicit_auto_approve_min_confidence
        4. Category is in explicit_auto_approve_allowed_categories
        5. Privacy/security guards already passed (caller ensures this)
        6. Dedup already passed (caller ensures this)

        If any condition fails, the suggestion stays in pending.

        Returns True if the suggestion was auto-approved and written.
        """
        if not self.explicit_auto_approve_enabled:
            logger.debug("Auto-approve disabled, keeping as pending")
            return False

        # Must be an explicit remember (not auto-extracted)
        if suggestion.reason != "explicit_remember":
            logger.debug(
                "Non-explicit suggestion (reason=%s), keeping as pending",
                suggestion.reason,
            )
            return False

        # Confidence threshold
        if suggestion.confidence < self.explicit_auto_approve_min_confidence:
            logger.debug(
                "Confidence %.2f < threshold %.2f, keeping as pending",
                suggestion.confidence,
                self.explicit_auto_approve_min_confidence,
            )
            return False

        # Category allowlist
        if suggestion.category.value not in self.explicit_auto_approve_allowed_categories:
            logger.debug(
                "Category %s not in allowlist, keeping as pending",
                suggestion.category.value,
            )
            return False

        # Security guard: double-check before writing
        security_decision = self.security_guard.guard(suggestion.content)
        if security_decision.action == "block":
            logger.debug(
                "Security guard blocked auto-approve, keeping as pending"
            )
            return False

        # Auto-approve: use existing accept() path (never direct repo write)
        logger.info(
            "Auto-approving explicit remember: %s (conf=%.2f, cat=%s)",
            suggestion.content[:60],
            suggestion.confidence,
            suggestion.category.value,
        )
        try:
            result = self.accept(suggestion)
            if result is not None:
                return True
            logger.warning(
                "Auto-approve accept() returned None, keeping as pending"
            )
            return False
        except Exception as exc:
            logger.warning(
                "Auto-approve write failed (%s), keeping as pending: %s",
                type(exc).__name__,
                exc,
            )
            return False

    # --------------------------------------------------------- internal helpers
    def _is_privacy_violation(self, content: str) -> bool:
        """Check if content violates privacy red-line rules."""
        if not isinstance(content, str) or not content.strip():
            return True
        # Check write_guards red-line detector
        violation = detect_red_line(content)
        if violation is not None:
            return True
        return False

    def _security_guard_candidate(self, suggestion: MemorySuggestion) -> GuardDecision | None:
        """Run the optional second-layer security guard on a candidate.

        Returns the GuardDecision if allowed, or None if blocked.
        """
        decision = self.security_guard.guard(suggestion.content)
        if decision.action == "block":
            return None
        if decision.action == "redact" and decision.content is not None:
            redacted = decision.content.strip()
            if redacted:
                # Update suggestion content in-place via new suggestion
                pass  # We keep the original content; redaction happens at write time
        return decision

    def _dedup_candidates(
        self, candidates: list[MemorySuggestion]
    ) -> list[MemorySuggestion]:
        """Remove candidates that duplicate existing pending suggestions or MemoryRecords.

        Dedup pipeline:
        1. Layer 1: normalised exact dedup (existing).
        2. Layer 2: semantic dedup within same category (new).
        3. Also checks existing MemoryRecords for semantic duplicates.
        """
        if not candidates:
            return []

        # Build set of normalized content from existing pending
        existing_pending_norm: set[str] = set()
        for s in self._pending:
            existing_pending_norm.add(self._normalize_for_compare(s.content))

        # Also check existing MemoryRecords if memory_service supports it
        existing_records_norm: set[str] = set()
        existing_records: list[MemoryRecord] = []
        try:
            list_method = getattr(self.memory_service, "list", None)
            if callable(list_method):
                for record in list_method():
                    if isinstance(record, MemoryRecord):
                        existing_records_norm.add(
                            self._normalize_for_compare(record.content)
                        )
                        existing_records.append(record)
        except Exception:
            pass  # Best-effort; don't fail dedup on memory_service errors

        normalized_seen: set[str] = set()
        deduped: list[MemorySuggestion] = []
        for candidate in candidates:
            norm = self._normalize_for_compare(candidate.content)
            if norm in existing_pending_norm or norm in existing_records_norm:
                logger.debug(
                    "Candidate suppressed (exact duplicate): %s", candidate.content[:50]
                )
                continue
            if norm in normalized_seen:
                continue
            normalized_seen.add(norm)

            # Layer 2: semantic dedup
            if self.semantic_dedup is not None:
                try:
                    # Check against existing pending (same category)
                    same_cat_pending = [
                        s for s in self._pending
                        if s.category == candidate.category
                    ]
                    semantic_match = self.semantic_dedup.check_pending(
                        candidate, same_cat_pending
                    )
                    if semantic_match is not None and semantic_match.is_duplicate:
                        logger.debug(
                            "Candidate suppressed (semantic duplicate of pending): %s",
                            candidate.content[:50],
                        )
                        continue

                    # Check against existing MemoryRecords (same category)
                    same_cat_records = [
                        r for r in existing_records
                        if r.category == candidate.category
                    ]
                    semantic_match = self.semantic_dedup.check_existing_records(
                        candidate, same_cat_records
                    )
                    if semantic_match is not None and semantic_match.is_duplicate:
                        logger.debug(
                            "Candidate suppressed (semantic duplicate of record): %s",
                            candidate.content[:50],
                        )
                        continue
                except Exception as exc:
                    logger.debug(
                        "Semantic dedup comparison failed, falling back to exact dedup: %s",
                        exc,
                    )

            deduped.append(candidate)

        return deduped

    def _has_existing_memory(self, suggestion: MemorySuggestion) -> bool:
        """Check if a suggestion duplicates any existing MemoryRecord (exact + semantic)."""
        try:
            list_method = getattr(self.memory_service, "list", None)
            if not callable(list_method):
                return False
            norm = self._normalize_for_compare(suggestion.content)
            for record in list_method():
                if isinstance(record, MemoryRecord):
                    if self._normalize_for_compare(record.content) == norm:
                        return True
        except Exception:
            pass

        # Layer 2: semantic check against existing records
        if self.semantic_dedup is not None:
            try:
                existing_records: list[MemoryRecord] = []
                list_method = getattr(self.memory_service, "list", None)
                if callable(list_method):
                    for record in list_method():
                        if isinstance(record, MemoryRecord):
                            existing_records.append(record)
                same_cat_records = [
                    r for r in existing_records
                    if r.category == suggestion.category
                ]
                semantic_match = self.semantic_dedup.check_existing_records(
                    suggestion, same_cat_records
                )
                if semantic_match is not None and semantic_match.is_duplicate:
                    return True
            except Exception:
                pass  # Best-effort; don't fail

        return False

    @staticmethod
    def _normalize_for_compare(content: str) -> str:
        """Comparison-only normalization; never mutates stored content."""
        return "".join(ch for ch in content.casefold() if ch.isalnum())

    @staticmethod
    def _strip_remember_prefix(text: str) -> str:
        """Strip explicit remember prefixes from user text."""
        import re

        # Try longest/most-specific patterns first, then shorter ones
        prefixes = [
            # 请帮我记住一下 / 麻烦你记住一下
            r"^\s*(?:请|麻烦)?(?:你)?(?:帮我)?记住一下(?:这件事|这一点|这条)?[\s:：,，]*",
            # 请帮我记住下来 / 麻烦你记住下来
            r"^\s*(?:请|麻烦)?(?:你)?(?:帮我)?记住下来(?:这件事|这一点|这条)?[\s:：,，]*",
            # 请记住 / 请帮我记住 / 麻烦你记住 / 请帮我记一下 / 请帮我记下来
            r"^\s*(?:请|麻烦)?(?:你)?(?:帮我)?记(?:住|一下|下来)(?:这件事|这一点|这条)?[\s:：,，]*",
            # 请记住 / 请帮我记住 (shorter variants without 一下/下来)
            r"^\s*(?:请|麻烦)?(?:你)?(?:帮我)?记(?:住|下来)(?:这件事|这一点|这条)?[\s:：,，]*",
            # please remember that / please remember...
            r"^\s*(?:please\s+)?remember(?:\s+that)?[\s:：,，]*",
        ]
        for pattern in prefixes:
            result = re.sub(pattern, "", text, flags=re.IGNORECASE)
            if result != text:
                return result.strip()
        return text.strip()


__all__ = ["SuggestionService"]


def _category_or_project(value) -> MemoryCategory:
    try:
        return MemoryCategory(str(value))
    except (TypeError, ValueError):
        return MemoryCategory.PROJECT
