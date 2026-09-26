"""Qt-free routing models for Phase 9A: capabilities + recommendations.

Pure data contracts only. Nothing here starts a process, reads runtime state,
imports Qt, or touches the network. The deterministic router in
:mod:`core.agent_router` consumes these models; UI rendering is a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class TaskIntent(str, Enum):
    """Small, stable intent vocabulary. Deliberately not an NLP taxonomy."""

    CHAT = "chat"
    EXPLAIN = "explain"
    ANALYZE = "analyze"
    CODE = "code"
    REVIEW = "review"
    DEBUG = "debug"
    VISION = "vision"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


class HandoffMode(str, Enum):
    """How the recommended agent should actually be reached.

    Same agent can appear with different handoff modes: Claude for a short
    read is SHORT_TALK; Claude for a long/write task is OPEN_NATIVE.
    """

    SHORT_TALK = "short_talk"  # Firefly managed Short Talk (read-only analysis)
    OPEN_NATIVE = "open_native"  # open the agent's native surface
    UNAVAILABLE = "unavailable"  # no Firefly-managed route exists right now


class Confidence(str, Enum):
    """Coarse confidence only — never pseudo-precise scores."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReasonCode(str, Enum):
    """Stable machine-readable reason tokens. The UI translates these later."""

    USER_REQUESTED_AGENT = "user_requested_agent"
    OPEN_REQUESTED = "open_requested"
    VISION_MANAGED_UNAVAILABLE = "vision_managed_unavailable"
    BEST_FOR_CODING = "best_for_coding"
    BEST_FOR_REVIEW = "best_for_review"
    BEST_FOR_ANALYSIS = "best_for_analysis"
    NATIVE_SURFACE_REQUIRED = "native_surface_required"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    AMBIGUOUS_TASK = "ambiguous_task"
    ANALYSIS_FIRST = "analysis_first"
    CODING_CAPABILITY = "coding_capability"
    UNKNOWN_TASK = "unknown_task"


class AgentCapability(str, Enum):
    """What an agent can do in some Firefly-reachable execution mode.

    VISION is deliberately present but unassigned: no verified vision backend
    exists in the current runtime, and the registry must not fabricate it.
    """

    CHAT = "chat"
    ANALYSIS = "analysis"
    EXPLANATION = "explanation"
    CODING = "coding"
    REVIEW = "review"
    DEBUGGING = "debugging"
    VISION = "vision"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    TERMINAL = "terminal"
    LONG_TASK = "long_task"
    MANAGED_SHORT_TALK = "managed_short_talk"
    SESSION_RESUME = "session_resume"
    NATIVE_LAUNCH = "native_launch"
    LIFECYCLE_MONITOR = "lifecycle_monitor"


@dataclass(frozen=True, slots=True)
class TaskRequest:
    """Minimal structured description of what the user wants to do.

    Deliberately excludes: full conversation, agent session ids, API keys,
    raw provider state, QWidgets, and AgentEvents.
    """

    text: str
    workspace: str | None = None
    requested_agent: str | None = None
    intent: TaskIntent | None = None
    requires_write: bool | None = None
    prefer_low_cost: bool = True


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """Static capability truth for one agent.

    ``available`` answers "can Firefly currently execute a task through this
    agent" (not live state). ``managed_short_talk`` answers "does Firefly have
    an online-verified managed Short Talk backend". Capabilities list what the
    agent can do in some Firefly-reachable mode.
    """

    agent_id: str
    display_name: str
    capabilities: frozenset[AgentCapability] = frozenset()
    available: bool = True
    managed_short_talk: bool = False
    notes: tuple[str, ...] = ()

    def has(self, capability: AgentCapability) -> bool:
        return capability in self.capabilities


class CapabilityRegistry:
    """Static/local capability truth for each agent.

    This is *not* live state: working/waiting/success/error belongs to the
    StateMonitor. Profiles here answer "what can this agent do in some
    Firefly-reachable mode right now". The default registry encodes the
    audited, deliberately asymmetric truth for claude / codex / chatgpt.
    """

    def __init__(self, profiles: Mapping[str, AgentProfile] | None = None) -> None:
        self._profiles: dict[str, AgentProfile] = {}
        if profiles is not None:
            for agent_id, profile in profiles.items():
                self._profiles[str(agent_id).lower()] = profile

    def profile(self, agent_id: str) -> AgentProfile | None:
        return self._profiles.get(str(agent_id).lower())

    def agents(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def all(self) -> tuple[AgentProfile, ...]:
        return tuple(self._profiles[key] for key in self.agents())

    def has(self, agent_id: str, capability: AgentCapability) -> bool:
        profile = self.profile(agent_id)
        return profile is not None and profile.has(capability)

    @classmethod
    def build_default(cls) -> "CapabilityRegistry":
        """The audited truth for the current runtime (Phase 9A)."""
        claude = AgentProfile(
            agent_id="claude",
            display_name="Claude",
            capabilities=frozenset(
                {
                    AgentCapability.CHAT,
                    AgentCapability.ANALYSIS,
                    AgentCapability.EXPLANATION,
                    AgentCapability.REVIEW,
                    AgentCapability.FILE_READ,
                    AgentCapability.MANAGED_SHORT_TALK,
                    AgentCapability.SESSION_RESUME,
                    AgentCapability.NATIVE_LAUNCH,
                    AgentCapability.LIFECYCLE_MONITOR,
                }
            ),
            available=True,
            managed_short_talk=True,
            notes=(
                "Firefly managed Short Talk is read-only (--safe-mode + plan "
                "permission mode): no file writes, long agent loops, or "
                "approval flows.",
                "Long or write tasks route to the native Claude surface.",
            ),
        )
        codex = AgentProfile(
            agent_id="codex",
            display_name="Codex",
            capabilities=frozenset(
                {
                    AgentCapability.CODING,
                    AgentCapability.DEBUGGING,
                    AgentCapability.FILE_READ,
                    AgentCapability.FILE_WRITE,
                    AgentCapability.TERMINAL,
                    AgentCapability.LONG_TASK,
                    AgentCapability.MANAGED_SHORT_TALK,
                    AgentCapability.SESSION_RESUME,
                    AgentCapability.NATIVE_LAUNCH,
                    AgentCapability.LIFECYCLE_MONITOR,
                }
            ),
            available=True,
            managed_short_talk=True,
            notes=(
                "Firefly managed Short Talk is read-only (codex exec "
                "--sandbox read-only --json --ephemeral): no file writes or "
                "approval flows.",
                "Long or write tasks route to the native Codex surface.",
                "Thread resume is structurally supported but not "
                "online-verified; Short Talk uses ephemeral single-turn.",
            ),
        )
        chatgpt = AgentProfile(
            agent_id="chatgpt",
            display_name="ChatGPT",
            capabilities=frozenset({AgentCapability.NATIVE_LAUNCH}),
            available=False,
            managed_short_talk=False,
            notes=(
                "Firefly has no managed backend; only the dock/URL entry.",
                "Never fabricate direct chat or task execution.",
            ),
        )
        return cls({"claude": claude, "codex": codex, "chatgpt": chatgpt})


DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry.build_default()


@dataclass(frozen=True, slots=True)
class AgentRecommendation:
    """One ranked recommendation. Core emits reason tokens, never prose."""

    agent_id: str
    score: int
    reason_code: ReasonCode
    confidence: Confidence
    intent: TaskIntent
    handoff_mode: HandoffMode
    requires_confirmation: bool = False
    multi_step_candidate: bool = False
