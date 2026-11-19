"""Shared Runtime infrastructure for all producers, including assembled plugins."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from .domain import AgentEvent, JSONValue
from .observability.event_hub import EventHub
from .observability.events import CallbackEventSink, EventSinkFailure, ensure_isolated_event_sink
from .ports import EventSink, TelemetrySink
from .observability.telemetry import _project
from .observability.usage import PricingTable, UsageLedger, UsageSummary


class _RuntimeObservation:
    def __init__(
        self,
        event_sink: EventSink | None,
        telemetry: TelemetrySink | None,
        pricing: PricingTable | None,
        model: str | None,
    ) -> None:
        self.hub = EventHub()
        self._metadata: ContextVar[Mapping[str, JSONValue]] = ContextVar(
            "session_observation_metadata", default={}
        )
        self.ledger = UsageLedger(pricing, default_model=model)
        self._accounting = ensure_isolated_event_sink(self.ledger)
        self.sink = ensure_isolated_event_sink(self)
        self._application = ensure_isolated_event_sink(event_sink)
        self._telemetry = (
            ensure_isolated_event_sink(
                CallbackEventSink(lambda event: telemetry.on_event(_project(event)))
            )
            if telemetry is not None
            else None
        )

    @property
    def failures(self) -> tuple[EventSinkFailure, ...]:
        telemetry = self._telemetry.failures if self._telemetry is not None else ()
        return (
            self.sink.failures
            + self._accounting.failures
            + self._application.failures
            + telemetry
        )

    @contextmanager
    def session_scope(self, metadata: Mapping[str, JSONValue]) -> Iterator[None]:
        """Carry controller metadata into its dispatcher and plugin child tasks."""
        token = self._metadata.set(metadata)
        try:
            yield
        finally:
            self._metadata.reset(token)

    def session_sink(
        self, metadata: Mapping[str, JSONValue], *, use_default_pricing: bool
    ) -> EventSink:
        """Bind direct controller events, including a handle aborted by another task."""

        def emit(event: AgentEvent) -> None:
            if event.kind == "model_usage" and not use_default_pricing:
                event = replace(event, payload={**event.payload, "model": None})
            with self.session_scope(metadata):
                self.sink.emit(event)

        return CallbackEventSink(emit)

    def emit(self, event: AgentEvent) -> None:
        metadata = self._metadata.get()
        if event.envelope.session_id is not None and metadata:
            event = replace(event, envelope=replace(event.envelope, metadata=metadata))
        self._accounting.emit(event)
        if event.kind == "run_completed":
            envelope = event.envelope
            if envelope.session_id is not None and envelope.run_id is not None:
                summary = (
                    self.ledger.for_run(envelope.session_id, envelope.run_id)
                    or UsageSummary()
                )
                cost = summary.estimated_cost_usd
                event = replace(
                    event,
                    payload={
                        **event.payload,
                        "estimated_cost_usd": str(cost) if cost is not None else None,
                        "unpriced_requests": summary.unpriced_requests,
                    },
                )
        self.hub.emit(event)
        if self._telemetry is not None:
            self._telemetry.emit(event)
        self._application.emit(event)
