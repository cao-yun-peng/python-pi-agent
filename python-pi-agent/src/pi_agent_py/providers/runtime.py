"""Retry, backoff, routing, and attempt tracing above raw model providers."""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from uuid import uuid4

from ..domain import (
    EventKind,
    Message,
    ModelStreamEvent,
    ProviderRequestContext,
    ToolDefinition,
)
from ..errors import (
    ProviderExhaustedError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderRoutingError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from ..observability.events import EventSinkFailure, ensure_isolated_event_sink, make_agent_event
from .. import ports as _ports
from ..ports import BackoffStrategy, EventSink, ModelProvider, RetryPolicy, RoutePolicy

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
RandomUniform = Callable[[float, float], float]


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    """Secret-free final record retained by ProviderExhaustedError."""

    provider_name: str
    attempt: int
    error_type: str
    retryable: bool
    output_started: bool
    duration_ms: float


class DefaultRetryPolicy:
    """Retry transient pre-output failures, then allow the next candidate."""

    def __init__(self, *, max_attempts_per_provider: int = 2) -> None:
        if type(max_attempts_per_provider) is not int or max_attempts_per_provider <= 0:
            raise ValueError("max_attempts_per_provider must be a positive integer")
        self._max_attempts_per_provider = max_attempts_per_provider

    def decide(self, failure: _ports.AttemptFailure) -> _ports.RetryDecision:
        if failure.output_started:
            return _ports.RetryDecision("fail", "provider output already started")
        if not failure.retryable:
            return _ports.RetryDecision("fail", "provider failure is not retryable")
        if failure.provider_attempt < self._max_attempts_per_provider:
            return _ports.RetryDecision("retry_same", "retryable provider failure")
        return _ports.RetryDecision("try_next", "provider retry budget exhausted")


class FullJitterBackoff:
    """Exponential backoff with full jitter and Retry-After support."""

    def __init__(
        self,
        *,
        base_delay_seconds: float = 0.25,
        max_delay_seconds: float = 8.0,
        random_uniform: RandomUniform = random.uniform,
    ) -> None:
        for name, value in (
            ("base_delay_seconds", base_delay_seconds),
            ("max_delay_seconds", max_delay_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")
        self._base = float(base_delay_seconds)
        self._maximum = float(max_delay_seconds)
        self._random_uniform = random_uniform

    def delay_seconds(self, failure: _ports.AttemptFailure) -> float:
        retry_after = getattr(failure.error, "retry_after_seconds", None)
        if retry_after is not None:
            return min(self._maximum, max(0.0, float(retry_after)))
        upper = min(
            self._maximum,
            self._base * (2 ** max(0, failure.provider_attempt - 1)),
        )
        delay = float(self._random_uniform(0.0, upper))
        if not math.isfinite(delay) or delay < 0 or delay > upper:
            raise ValueError("random_uniform returned an invalid backoff delay")
        return delay


class CapabilityRoutePolicy:
    """Filter explicit candidates by purpose, tools, and opaque state ownership."""

    def select(
        self,
        candidates: Sequence[_ports.ProviderCandidate],
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        context: ProviderRequestContext,
    ) -> Sequence[_ports.ProviderCandidate]:
        state_owners = _latest_provider_state_owners(messages)
        return tuple(
            candidate
            for candidate in candidates
            if context.purpose in candidate.purposes
            and (not tools or candidate.supports_tools)
            and (
                not state_owners
                or state_owners <= candidate.state_owners
            )
        )


class DirectModelRuntime:
    """Trace one raw provider attempt without retrying or rewriting errors."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        name: str = "direct",
        event_sink: EventSink | None = None,
        clock: Clock = time.perf_counter,
    ) -> None:
        if not name.strip():
            raise ValueError("provider name must not be empty")
        self._provider = provider
        self._name = name
        self._event_sink = ensure_isolated_event_sink(event_sink)
        self._clock = clock

    @property
    def event_sink_failures(self) -> tuple[EventSinkFailure, ...]:
        return self._event_sink.failures

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        _emit_provider_event(
            self._event_sink,
            "provider_route_selected",
            context,
            candidate_names=(self._name,),
        )
        started_at = self._clock()
        output_started = False
        _emit_provider_event(
            self._event_sink,
            "provider_attempt_started",
            context,
            provider_name=self._name,
            provider_attempt=1,
            total_attempt=1,
            span_id=context.attempt_id,
            parent_span_id=context.request_id,
        )
        try:
            async for event in self._provider.stream(
                messages,
                tools,
                context=context,
            ):
                output_started = True
                yield event
        except asyncio.CancelledError:
            _emit_provider_event(
                self._event_sink,
                "provider_attempt_failed",
                context,
                provider_name=self._name,
                provider_attempt=1,
                total_attempt=1,
                outcome="cancelled",
                retryable=False,
                output_started=output_started,
                error="CancelledError",
                span_id=context.attempt_id,
                parent_span_id=context.request_id,
                duration_ms=_duration_ms(self._clock, started_at),
            )
            raise
        except Exception as error:
            _emit_provider_event(
                self._event_sink,
                "provider_attempt_failed",
                context,
                provider_name=self._name,
                provider_attempt=1,
                total_attempt=1,
                outcome="error",
                retryable=_is_retryable(error),
                output_started=output_started,
                error=type(error).__name__,
                span_id=context.attempt_id,
                parent_span_id=context.request_id,
                duration_ms=_duration_ms(self._clock, started_at),
            )
            raise
        else:
            _emit_provider_event(
                self._event_sink,
                "provider_attempt_succeeded",
                context,
                provider_name=self._name,
                provider_attempt=1,
                total_attempt=1,
                outcome="success",
                span_id=context.attempt_id,
                parent_span_id=context.request_id,
                duration_ms=_duration_ms(self._clock, started_at),
            )


class ResilientModelRuntime:
    """A logical ModelProvider composed from retryable raw providers."""

    def __init__(
        self,
        candidates: Sequence[_ports.ProviderCandidate],
        *,
        retry_policy: RetryPolicy | None = None,
        backoff: BackoffStrategy | None = None,
        route_policy: RoutePolicy | None = None,
        event_sink: EventSink | None = None,
        max_total_attempts: int = 4,
        max_elapsed_seconds: float = 30.0,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.perf_counter,
    ) -> None:
        normalized = tuple(candidates)
        if not normalized:
            raise ValueError("at least one provider candidate is required")
        names = [candidate.name for candidate in normalized]
        if len(set(names)) != len(names):
            raise ValueError("provider candidate names must be unique")
        if type(max_total_attempts) is not int or max_total_attempts <= 0:
            raise ValueError("max_total_attempts must be a positive integer")
        if (
            not isinstance(max_elapsed_seconds, (int, float))
            or isinstance(max_elapsed_seconds, bool)
            or not math.isfinite(max_elapsed_seconds)
            or max_elapsed_seconds <= 0
        ):
            raise ValueError("max_elapsed_seconds must be a finite positive number")
        self._candidates = normalized
        self._retry_policy = retry_policy or DefaultRetryPolicy()
        self._backoff = backoff or FullJitterBackoff()
        self._route_policy = route_policy or CapabilityRoutePolicy()
        self._event_sink = ensure_isolated_event_sink(event_sink)
        self._max_total_attempts = max_total_attempts
        self._max_elapsed_seconds = float(max_elapsed_seconds)
        self._sleep = sleep
        self._clock = clock

    @property
    def event_sink_failures(self) -> tuple[EventSinkFailure, ...]:
        return self._event_sink.failures

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        selected = tuple(
            self._route_policy.select(
                self._candidates,
                messages,
                tools,
                context=context,
            )
        )
        if not selected:
            raise ProviderRoutingError(
                "no provider candidate supports this request purpose, tool set, "
                "and provider-owned continuity state"
            )
        known = {candidate.name: candidate for candidate in self._candidates}
        if any(known.get(candidate.name) is not candidate for candidate in selected):
            raise ProviderRoutingError(
                "route policy returned an unknown or replaced provider candidate"
            )

        self._emit(
            "provider_route_selected",
            context,
            candidate_names=tuple(candidate.name for candidate in selected),
        )
        started_at = self._clock()
        total_attempt = 0
        summaries: list[AttemptSummary] = []
        last_error: Exception | None = None
        previous: _ports.ProviderCandidate | None = None

        for candidate in selected:
            if (
                total_attempt >= self._max_total_attempts
                or self._clock() - started_at >= self._max_elapsed_seconds
            ):
                break
            if previous is not None:
                self._emit(
                    "provider_fallback_selected",
                    context,
                    from_provider=previous.name,
                    to_provider=candidate.name,
                    reason=type(last_error).__name__ if last_error else "route",
                )
            previous = candidate
            provider_attempt = 0

            while total_attempt < self._max_total_attempts:
                if self._clock() - started_at >= self._max_elapsed_seconds:
                    break
                provider_attempt += 1
                total_attempt += 1
                attempt_context = replace(
                    context,
                    attempt_id=(
                        context.attempt_id if total_attempt == 1 else uuid4().hex
                    ),
                    attempt=context.attempt + total_attempt - 1,
                )
                attempt_started_at = self._clock()
                output_started = False
                self._emit(
                    "provider_attempt_started",
                    attempt_context,
                    provider_name=candidate.name,
                    provider_attempt=provider_attempt,
                    total_attempt=total_attempt,
                    span_id=attempt_context.attempt_id,
                    parent_span_id=context.request_id,
                )
                try:
                    async for event in candidate.provider.stream(
                        messages,
                        tools,
                        context=attempt_context,
                    ):
                        output_started = True
                        yield event
                except asyncio.CancelledError:
                    self._emit(
                        "provider_attempt_failed",
                        attempt_context,
                        provider_name=candidate.name,
                        provider_attempt=provider_attempt,
                        total_attempt=total_attempt,
                        outcome="cancelled",
                        retryable=False,
                        output_started=output_started,
                        error="CancelledError",
                        span_id=attempt_context.attempt_id,
                        parent_span_id=context.request_id,
                        duration_ms=_duration_ms(self._clock, attempt_started_at),
                    )
                    raise
                except Exception as error:
                    last_error = error
                    retryable = _is_retryable(error)
                    duration_ms = _duration_ms(self._clock, attempt_started_at)
                    summaries.append(
                        AttemptSummary(
                            provider_name=candidate.name,
                            attempt=attempt_context.attempt,
                            error_type=type(error).__name__,
                            retryable=retryable,
                            output_started=output_started,
                            duration_ms=duration_ms,
                        )
                    )
                    self._emit(
                        "provider_attempt_failed",
                        attempt_context,
                        provider_name=candidate.name,
                        provider_attempt=provider_attempt,
                        total_attempt=total_attempt,
                        outcome="error",
                        retryable=retryable,
                        output_started=output_started,
                        error=type(error).__name__,
                        span_id=attempt_context.attempt_id,
                        parent_span_id=context.request_id,
                        duration_ms=duration_ms,
                    )
                    failure = _ports.AttemptFailure(
                        provider_name=candidate.name,
                        error=error,
                        retryable=retryable,
                        output_started=output_started,
                        provider_attempt=provider_attempt,
                        total_attempt=total_attempt,
                        elapsed_seconds=max(0.0, self._clock() - started_at),
                    )
                    decision = self._retry_policy.decide(failure)
                    if not isinstance(decision, _ports.RetryDecision):
                        raise TypeError("retry policy must return RetryDecision")
                    if output_started or decision.action == "fail":
                        raise
                    if decision.action == "try_next":
                        break
                    if total_attempt >= self._max_total_attempts:
                        break
                    delay = self._backoff.delay_seconds(failure)
                    if not math.isfinite(delay) or delay < 0:
                        raise ValueError(
                            "backoff strategy must return a finite non-negative delay"
                        )
                    elapsed = max(0.0, self._clock() - started_at)
                    if elapsed + delay >= self._max_elapsed_seconds:
                        break
                    self._emit(
                        "provider_retry_scheduled",
                        attempt_context,
                        provider_name=candidate.name,
                        provider_attempt=provider_attempt,
                        total_attempt=total_attempt,
                        delay_seconds=delay,
                        reason=decision.reason,
                        parent_span_id=context.request_id,
                    )
                    await self._sleep(delay)
                    continue
                else:
                    self._emit(
                        "provider_attempt_succeeded",
                        attempt_context,
                        provider_name=candidate.name,
                        provider_attempt=provider_attempt,
                        total_attempt=total_attempt,
                        outcome="success",
                        span_id=attempt_context.attempt_id,
                        parent_span_id=context.request_id,
                        duration_ms=_duration_ms(self._clock, attempt_started_at),
                    )
                    return
                break

            if total_attempt >= self._max_total_attempts:
                break

        detail = ", ".join(
            f"{item.provider_name}:{item.error_type}" for item in summaries
        ) or "no attempt completed"
        exhausted = ProviderExhaustedError(
            f"model request exhausted after {len(summaries)} failed attempts: {detail}",
            attempts=tuple(summaries),
        )
        if last_error is None:
            raise exhausted
        raise exhausted from last_error

    def _emit(
        self,
        kind: EventKind,
        context: ProviderRequestContext,
        *,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        duration_ms: float | None = None,
        **payload: object,
    ) -> None:
        _emit_provider_event(
            self._event_sink,
            kind,
            context,
            span_id=span_id,
            parent_span_id=parent_span_id,
            duration_ms=duration_ms,
            **payload,
        )


def _latest_provider_state_owners(messages: Sequence[Message]) -> frozenset[str]:
    for message in reversed(messages):
        if message.role == "assistant" and message.provider_state:
            return frozenset(state.provider for state in message.provider_state)
    return frozenset()


def _is_retryable(error: Exception) -> bool:
    if isinstance(
        error,
        (ProviderRateLimitError, ProviderTimeoutError, ProviderTransportError),
    ):
        return True
    if isinstance(error, ProviderHTTPError):
        return error.retryable
    return bool(getattr(error, "retryable", False))


def _duration_ms(clock: Clock, started_at: float) -> float:
    return max(0.0, (clock() - started_at) * 1000)


def _emit_provider_event(
    event_sink: EventSink,
    kind: EventKind,
    context: ProviderRequestContext,
    *,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    duration_ms: float | None = None,
    **payload: object,
) -> None:
    event_sink.emit(
        make_agent_event(
            kind,
            "capability",
            source="runtime",
            payload=payload,
            session_id=context.session_id,
            run_id=context.run_id,
            turn_id=context.turn_id,
            request_id=context.request_id,
            attempt_id=(
                context.attempt_id
                if kind.startswith("provider_attempt")
                or kind == "provider_retry_scheduled"
                else None
            ),
            span_id=span_id,
            parent_span_id=parent_span_id,
            duration_ms=duration_ms,
        )
    )
