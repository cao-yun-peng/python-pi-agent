"""Typed, ordered transforms at model-request and executed-result boundaries."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TypeVar
from uuid import uuid4

from ..domain import (
    ContextPlan,
    EventKind,
    ProviderRequestContext,
    ToolOutput,
    ToolResultContext,
)
from ..errors import MiddlewareError
from ..observability.events import make_agent_event
from ..ports import EventSink, ProviderRequestHook, ToolResultHook

T = TypeVar("T", ContextPlan, ToolOutput)


@dataclass(frozen=True, slots=True)
class Middleware:
    """Explicit stage implementations; resource ownership stays with the creator."""

    name: str
    provider_request: ProviderRequestHook | None = None
    tool_result: ToolResultHook | None = None
    priority: int = 0
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("middleware name must not be empty")
        if self.provider_request is None and self.tool_result is None:
            raise ValueError("middleware requires at least one stage")
        if type(self.priority) is not int:
            raise TypeError("middleware priority must be an integer")
        timeout = self.timeout_seconds
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("middleware timeout must be finite and non-negative")


class _MiddlewareChain:
    def __init__(self, middleware: Sequence[Middleware], sink: EventSink) -> None:
        self._items = tuple(sorted(middleware, key=lambda item: item.priority))
        self._sink = sink

    async def prepare(
        self, context: ProviderRequestContext, plan: ContextPlan
    ) -> ContextPlan:
        current = plan
        for item in self._items:
            hook = item.provider_request
            if hook is None:
                continue
            current = await self._invoke(
                item,
                context,
                current,
                lambda value: hook.prepare(context, value),
                lambda value: (
                    isinstance(value, ContextPlan)
                    and value.source_message_count == current.source_message_count
                ),
            )
        return current

    async def transform(
        self, context: ToolResultContext, output: ToolOutput
    ) -> ToolOutput:
        current = output
        names: list[str] = []
        for item in self._items:
            hook = item.tool_result
            if hook is None:
                continue
            names.append(item.name)
            try:
                current = await self._invoke(
                    item,
                    context,
                    current,
                    lambda value: hook.transform(deepcopy(context), value),
                    lambda value: (
                        isinstance(value, ToolOutput)
                        and value.is_error is output.is_error
                    ),
                )
            except MiddlewareError:
                current = ToolOutput(
                    "The tool ran, but its result could not be processed. "
                    "The outcome is unknown; do not assume it failed and do not retry automatically.",
                    is_error=True,
                    structured_content={
                        "status": "middleware_failed",
                        "middleware": item.name,
                        "tool_outcome": context.outcome,
                    },
                )
                break
        if names:
            self._emit(
                "tool_result_transformed",
                context,
                middleware_names=names,
                changed=current != output,
                original_content_chars=len(output.content),
                final_content_chars=len(current.content),
            )
        return current

    async def _invoke(
        self,
        item: Middleware,
        context: ProviderRequestContext | ToolResultContext,
        value: T,
        call: Callable[[T], Awaitable[T]],
        valid: Callable[[object], bool],
    ) -> T:
        span_id, started = uuid4().hex, time.perf_counter()
        self._emit(
            "middleware_started", context, span_id=span_id, middleware_name=item.name
        )
        try:
            copied = deepcopy(value)
            if item.timeout_seconds is None:
                result = await call(copied)
            else:
                async with asyncio.timeout(item.timeout_seconds):
                    result = await call(copied)
            if not valid(result):
                raise TypeError("middleware returned an invalid stage result")
            result = deepcopy(result)
        except asyncio.CancelledError:
            self._emit(
                "middleware_cancelled",
                context,
                span_id=span_id,
                middleware_name=item.name,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        except Exception as error:
            self._emit(
                "middleware_failed",
                context,
                span_id=span_id,
                middleware_name=item.name,
                duration_ms=(time.perf_counter() - started) * 1000,
                error=type(error).__name__,
            )
            raise MiddlewareError("middleware stage failed") from error
        self._emit(
            "middleware_finished",
            context,
            span_id=span_id,
            middleware_name=item.name,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    def _emit(
        self,
        kind: EventKind,
        context: ProviderRequestContext | ToolResultContext,
        *,
        span_id: str | None = None,
        duration_ms: float | None = None,
        **payload: object,
    ) -> None:
        request_id: str | None
        attempt_id: str | None
        if isinstance(context, ProviderRequestContext):
            stage, session_id = "provider_request", context.session_id
            request_id, attempt_id = context.request_id, context.attempt_id
        else:
            stage, session_id = "tool_result", context.execution.session_id
            request_id = attempt_id = None
            payload.update(
                tool_call_id=context.execution.call.id,
                tool_name=context.execution.call.name,
            )
        self._sink.emit(
            make_agent_event(
                kind,
                "capability",
                source="middleware",
                payload={"stage": stage, **payload},
                session_id=session_id,
                run_id=context.run_id,
                turn_id=context.turn_id,
                request_id=request_id,
                attempt_id=attempt_id,
                span_id=span_id,
                duration_ms=duration_ms,
            )
        )
