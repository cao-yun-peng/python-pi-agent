"""Shared Runtime infrastructure for all producers, including assembled plugins."""

from __future__ import annotations

from dataclasses import replace

from .domain import AgentEvent
from .event_hub import EventHub
from .events import CallbackEventSink, EventSinkFailure, ensure_isolated_event_sink
from .ports import EventSink, TelemetrySink
from .telemetry import _project
from .usage import PricingTable, UsageLedger, UsageSummary


class _RuntimeObservation:
    def __init__(
        self,
        event_sink: EventSink | None,
        telemetry: TelemetrySink | None,
        pricing: PricingTable | None,
        model: str | None,
    ) -> None:
        self.hub = EventHub()
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

    def emit(self, event: AgentEvent) -> None:
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
