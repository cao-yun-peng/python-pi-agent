"""Optional OpenTelemetry adapter. Install pi-agent-py[otel] to import it."""

from __future__ import annotations

from dataclasses import dataclass, field

from opentelemetry.context import Context
from opentelemetry.metrics import Meter
from opentelemetry.trace import Span, Status, StatusCode, Tracer, set_span_in_context

from ..domain import AgentEventEnvelope, EventKind, TelemetryEvent

_START: dict[EventKind, str] = {
    "model_request_started": "model_request",
    "provider_attempt_started": "provider_attempt",
    "tool_execution_started": "tool",
    "compaction_started": "compaction",
    "plugin_hook_started": "hook",
}
_END: dict[EventKind, str] = {
    "model_request_finished": "success",
    "model_request_failed": "error",
    "provider_attempt_succeeded": "success",
    "provider_attempt_failed": "error",
    "tool_execution_finished": "success",
    "compaction_completed": "success",
    "compaction_failed": "error",
    "compaction_cancelled": "cancelled",
    "plugin_hook_finished": "success",
    "plugin_hook_failed": "error",
    "plugin_hook_cancelled": "cancelled",
}
_RUN_OUTCOME: dict[EventKind, str] = {
    "run_completed": "success",
    "run_failed": "error",
    "run_cancelled": "cancelled",
}


@dataclass(slots=True)
class _Span:
    span: Span
    started: int

    def end(self, when: int, outcome: str) -> None:
        self.span.set_attribute("pi_agent.outcome", outcome)
        if outcome == "success":
            self.span.set_status(Status(StatusCode.OK))
        elif outcome != "cancelled":
            self.span.set_status(Status(StatusCode.ERROR))
        self.span.end(end_time=max(self.started, when))


@dataclass(slots=True)
class _RunSpans:
    root: _Span | None = None
    turn: _Span | None = None
    turn_outcome: str = "success"
    seen_turns: set[str] = field(default_factory=set)
    children: dict[str, _Span] = field(default_factory=dict)
    outcome: str = "error"
    last_event: int = 0

    def finish(self, when: int) -> None:
        outcome = "cancelled" if self.outcome == "cancelled" else "error"
        for span in reversed(tuple(self.children.values())):
            span.end(when, outcome)
        self.children.clear()
        if self.turn is not None:
            self.turn.end(
                when, self.turn_outcome if self.outcome == "success" else self.outcome
            )
        if self.root is not None:
            self.root.end(when, self.outcome)


class OtelTelemetrySink:
    """Map events to traces and low-cardinality metrics using borrowed SDK handles."""

    def __init__(
        self,
        tracer: Tracer,
        meter: Meter,
        *,
        tool_names: frozenset[str] = frozenset(),
        provider_names: frozenset[str] = frozenset(),
    ) -> None:
        self._tracer = tracer
        self._tools = frozenset(tool_names)
        self._providers = frozenset(provider_names)
        self._runs: dict[tuple[str | None, str | None], _RunSpans] = {}
        self._duration = meter.create_histogram(
            "pi_agent.model.request.duration", unit="ms"
        )
        self._tokens = meter.create_counter("pi_agent.tokens", unit="{token}")
        self._executions = meter.create_counter("pi_agent.tool.executions")
        self._attempts = meter.create_counter("pi_agent.provider.attempts")
        self._retries = meter.create_counter("pi_agent.provider.retries")
        self._fallbacks = meter.create_counter("pi_agent.provider.fallbacks")
        self._closed = False

    def on_event(self, event: TelemetryEvent) -> None:
        if self._closed:
            return
        self._record_metrics(event)
        env = event.envelope
        kind = event.kind
        key = (env.session_id, env.run_id)
        when = round(env.occurred_at * 1_000_000_000)
        if kind == "run_started":
            self._runs[key] = _RunSpans(
                root=self._start("run", when, env), last_event=when
            )
            return
        if kind == "run_settled":
            run = self._runs.pop(key, None)
            if run is not None:
                run.finish(when)
            return

        run = self._runs.get(key)
        if run is None:
            if kind not in _START or env.span_id is None:
                return
            # Manual compaction and other standalone operations have no run root.
            run = self._runs.setdefault(key, _RunSpans())
        run.last_event = max(run.last_event, when)
        if kind in _RUN_OUTCOME:
            run.outcome = _RUN_OUTCOME[kind]
        if (
            run.root is not None
            and env.turn_id is not None
            and env.turn_id not in run.seen_turns
        ):
            if run.turn is not None:
                run.turn.end(when, run.turn_outcome)
            run.turn = self._start("turn", when, env, run.root)
            run.seen_turns.add(env.turn_id)
            run.turn_outcome = "success"

        if env.span_id is not None and kind in _START:
            parent = run.children.get(env.parent_span_id or "") or run.turn or run.root
            run.children[env.span_id] = self._start(_START[kind], when, env, parent)
        elif env.span_id is not None and kind in _END:
            span = run.children.pop(env.span_id, None)
            if span is not None:
                end = (
                    when
                    if env.duration_ms is None
                    else span.started + round(env.duration_ms * 1e6)
                )
                outcome = event.outcome or _END[kind]
                span.end(end, outcome)
                if kind == "model_request_failed":
                    run.turn_outcome = outcome
            if run.root is None and not run.children:
                self._runs.pop(key, None)

    def _start(
        self, name: str, when: int, env: AgentEventEnvelope, parent: _Span | None = None
    ) -> _Span:
        context = Context()
        if parent is not None:
            context = set_span_in_context(parent.span, context)
        attributes = {
            f"pi_agent.{key}": value
            for key, value in (
                ("session_id", env.session_id),
                ("run_id", env.run_id),
                ("turn_id", env.turn_id),
                ("request_id", env.request_id),
                ("attempt_id", env.attempt_id),
            )
            if value is not None
        }
        return _Span(
            self._tracer.start_span(
                f"pi_agent.{name}",
                context=context,
                start_time=when,
                attributes=attributes,
            ),
            when,
        )

    def _record_metrics(self, event: TelemetryEvent) -> None:
        kind = event.kind
        provider = (
            event.provider_name
            if event.provider_name is not None
            and event.provider_name in self._providers
            else "other"
        )
        tool = (
            event.tool_name
            if event.tool_name is not None and event.tool_name in self._tools
            else "other"
        )
        outcome = event.outcome or _END.get(kind, "unknown")
        duration = event.envelope.duration_ms
        if (
            kind in ("model_request_finished", "model_request_failed")
            and duration is not None
        ):
            self._duration.record(duration, {"result": outcome})
        if event.usage is not None:
            for token_type, count in (
                ("input", event.usage.input_tokens),
                ("output", event.usage.output_tokens),
                ("cached", event.usage.cached_input_tokens),
            ):
                if count is not None:
                    self._tokens.add(count, {"type": token_type})
        if kind == "tool_execution_finished":
            self._executions.add(1, {"tool": tool, "result": outcome})
        if kind in ("provider_attempt_succeeded", "provider_attempt_failed"):
            self._attempts.add(1, {"provider": provider, "result": outcome})
        if kind == "provider_retry_scheduled":
            self._retries.add(1, {"provider": provider})
        if kind == "provider_fallback_selected":
            self._fallbacks.add(1, {"provider": provider})

    async def aclose(self) -> None:
        """End incomplete spans. The application owns SDK flush and shutdown."""
        self._closed = True
        runs = tuple(self._runs.values())
        self._runs.clear()
        for run in runs:
            run.outcome = "cancelled"
            run.finish(run.last_event)
