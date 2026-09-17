"""Minimal in-process runtime state/event fan-out for Firefly (Phase 1A).

RuntimeBus is a thin Qt Signal-backed pub/sub layer. StateMonitor remains
the sole authority for the pet's resolved state: this module never reads
``runtime/sources/*.json``, never writes ``runtime/state.json`` and never
calls :func:`state_broker.resolve`. It only re-publishes the payloads
publishers hand to it, so additive consumers (system tray, companion HUD,
...) can subscribe without new point-to-point connects in app.py.

Two channels:

- ``state``: :class:`RuntimeState` snapshots (``publish_state`` /
  ``subscribe_state``). Reserved for the resolved pet display state; only
  the StateMonitor forwarding line in app.py may publish here.
- ``event``: :class:`RuntimeEvent` envelopes (``publish_event`` /
  ``subscribe_event``). Generic channel for later domains (session,
  workflow, notification, agent turns, plugin status).

Delivery is QueuedConnection: every callback runs in the thread that called
subscribe (its relay object's thread), never in the publisher's thread.
``close()`` permanently stops delivery; publishing afterwards is a no-op.
The bus owns no files, no timers, no worker threads and calls no models.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt, Signal

if TYPE_CHECKING:
    from .models import ResolvedState


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """One resolved display-state snapshot for the ``state`` channel.

    Plain bus-level copy of the broker's ``ResolvedState``: ``state`` is the
    lifecycle value string and ``timestamp`` is the source-authoritative
    write time (not the bus arrival time), so downstream TTL rules keep
    working even when delivery is queued.
    """

    state: str
    agent_id: str | None
    source: str
    timestamp: int
    resolved_at: int

    @classmethod
    def from_resolved(cls, resolved: "ResolvedState") -> "RuntimeState":
        """Build from a ``core.models.ResolvedState`` (duck-typed copy)."""
        raw_state = resolved.state
        return cls(
            state=raw_state if isinstance(raw_state, str) else raw_state.value,
            agent_id=resolved.agent_id,
            source=resolved.source,
            timestamp=resolved.timestamp,
            resolved_at=resolved.resolved_at,
        )


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One generic runtime event envelope for the ``event`` channel.

    ``payload`` is always a frozen dataclass owned by its domain module;
    the bus never mutates or re-wraps it and raw provider JSON / bare dicts
    are never allowed on the bus. ``timestamp`` is the publish time in ms;
    a payload may carry its own more authoritative timestamp.
    """

    kind: str
    source: str
    timestamp: int = 0
    payload: Any = None


@dataclass(frozen=True, slots=True)
class CameraObservedEvent:
    """Vision-1B: one completed explicit camera observation ("看看我").

    Published on the event channel with ``kind="camera.observed"`` by the
    character runner after a camera vision turn succeeds. Carries only the
    final answer text — never pixels and never observation metadata that
    could leak into logs or memory.
    """

    text: str


class _Subscription(QObject):
    """Queued relay for one subscriber.

    The relay lives in the thread where ``subscribe`` was called; the bus
    connects to :meth:`_dispatch` with QueuedConnection, so the user
    callback always executes there — including when a worker thread
    publishes. A detached relay drops everything (unsubscribed / closed).
    """

    def __init__(self, callback: Callable[[Any], None], parent: QObject | None) -> None:
        super().__init__(parent)
        self._callback = callback
        self._active = True

    def _dispatch(self, payload: Any) -> None:
        if self._active:
            self._callback(payload)

    def detach(self) -> None:
        self._active = False


class RuntimeBus(QObject):
    """Qt Signal-backed runtime state/event fan-out.

    Publishing is synchronous into the Qt signal system; delivery to
    subscribers is always queued. ``close()`` is idempotent and makes every
    later publish a no-op; unsubscribing after close is safe.
    """

    _state_arrived = Signal(object)
    _event_arrived = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._closed = False
        self._state_subs: list[_Subscription] = []
        self._event_subs: list[_Subscription] = []

    # -- publish -----------------------------------------------------------

    def publish_state(self, state: RuntimeState) -> None:
        """Fan a :class:`RuntimeState` snapshot out to state subscribers."""
        if self._closed:
            return
        if not isinstance(state, RuntimeState):
            raise TypeError("publish_state expects a RuntimeState payload")
        self._state_arrived.emit(state)

    def publish_event(self, event: RuntimeEvent) -> None:
        """Fan a :class:`RuntimeEvent` envelope out to event subscribers."""
        if self._closed:
            return
        if not isinstance(event, RuntimeEvent):
            raise TypeError("publish_event expects a RuntimeEvent envelope")
        self._event_arrived.emit(event)

    # -- subscribe ----------------------------------------------------------

    def subscribe_state(self, callback: Callable[[RuntimeState], None]) -> Callable[[], None]:
        """Subscribe to state snapshots; returns an idempotent unsubscribe."""
        return self._subscribe(self._state_arrived, self._state_subs, callback)

    def subscribe_event(self, callback: Callable[[RuntimeEvent], None]) -> Callable[[], None]:
        """Subscribe to event envelopes; returns an idempotent unsubscribe."""
        return self._subscribe(self._event_arrived, self._event_subs, callback)

    def _subscribe(
        self,
        signal: Signal,
        bucket: list[_Subscription],
        callback: Callable[[Any], None],
    ) -> Callable[[], None]:
        if self._closed:
            return lambda: None
        relay = _Subscription(callback, parent=self)
        signal.connect(relay._dispatch, Qt.ConnectionType.QueuedConnection)
        bucket.append(relay)

        def unsubscribe() -> None:
            if relay not in bucket:
                return
            bucket.remove(relay)
            try:
                signal.disconnect(relay._dispatch)
            except (RuntimeError, TypeError):
                pass
            relay.detach()
            relay.deleteLater()

        return unsubscribe

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Stop delivery permanently; later publishes are no-ops."""
        if self._closed:
            return
        self._closed = True
        for signal, bucket in (
            (self._state_arrived, self._state_subs),
            (self._event_arrived, self._event_subs),
        ):
            for relay in list(bucket):
                try:
                    signal.disconnect(relay._dispatch)
                except (RuntimeError, TypeError):
                    pass
                relay.detach()
                relay.deleteLater()
            bucket.clear()
