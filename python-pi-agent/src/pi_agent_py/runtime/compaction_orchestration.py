"""Manual and automatic compaction for a durable Session."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from uuid import uuid4

from ..compaction import (
    CompactionPreparation,
    build_summary_message,
    prepare_compaction,
)
from ..components import CompactionComponents
from ..domain import (
    CompactionDecision,
    EventProvenance,
    Message,
    NextTurnContext,
    ProviderRequestContext,
    SessionCompaction,
    SessionEventContext,
    SessionSnapshot,
)
from ..errors import (
    AgentError,
    CompactionError,
    CompactionNotNeededError,
    CompactionPolicyError,
    ConfigurationError,
    ContextPreparationError,
)
from ..ports import NextTurnHook
from ._observation import _SessionEvents, _duration_ms
from ._persistence import _SessionWriter, _latest_durable_identity


class _SessionCompactor:
    """Evaluate compaction and commit its summary through a Session writer."""

    def __init__(
        self,
        session_id: str,
        compaction: CompactionComponents | None,
        events: _SessionEvents,
    ) -> None:
        self._session_id = session_id
        self._compaction = compaction
        self._events = events

    def next_turn_hook(
        self, writer: _SessionWriter, hook: NextTurnHook | None
    ) -> NextTurnHook | None:
        if self._compaction is None or self._compaction.policy is None:
            return hook
        compaction_hook = _SessionNextTurnHook(writer, self)
        if hook is None:
            return compaction_hook
        return _ChainedNextTurnHook((compaction_hook, hook))

    async def compact(
        self,
        load_writer: Callable[[], Awaitable[_SessionWriter]],
        *,
        keep_recent_turns: int,
    ) -> SessionSnapshot:
        try:
            writer = await load_writer()
            preparation = prepare_compaction(
                writer.snapshot.messages,
                keep_recent_turns=keep_recent_turns,
            )
        except asyncio.CancelledError:
            raise
        except AgentError as error:
            self._events.emit(
                "compaction_failed",
                error=type(error).__name__,
                automatic=False,
            )
            raise
        except Exception as error:
            wrapped = CompactionError("compaction preparation failed")
            self._events.emit(
                "compaction_failed",
                error=type(wrapped).__name__,
                automatic=False,
            )
            raise wrapped from error

        return await self._execute_compaction(
            writer,
            preparation,
            automatic=False,
            request_number=1,
            event_context=SessionEventContext(
                provenance=EventProvenance(
                    source="compaction",
                    actor_id="manual-compaction",
                )
            ),
        )

    async def maybe_auto_compact(
        self,
        writer: _SessionWriter,
        pending_messages: Sequence[Message] = (),
        *,
        request_number: int = 1,
        event_context: SessionEventContext | None = None,
    ) -> None:
        compaction = self._compaction
        if compaction is None or compaction.policy is None:
            return

        correlation_context = event_context or SessionEventContext(
            provenance=EventProvenance(
                source="compaction",
                actor_id="automatic-compaction",
            )
        )

        candidate_messages = (
            *writer.snapshot.messages,
            *pending_messages,
        )
        try:
            decision = compaction.policy.evaluate(candidate_messages)
            if not isinstance(decision, CompactionDecision):
                raise CompactionPolicyError(
                    "compaction policy must return CompactionDecision"
                )
        except CompactionError as error:
            self._events.emit(
                "compaction_failed",
                event_context=correlation_context,
                error=type(error).__name__,
                automatic=True,
                stage="policy",
            )
            raise
        except Exception as error:
            policy_error = CompactionPolicyError("compaction policy failed")
            self._events.emit(
                "compaction_failed",
                event_context=correlation_context,
                error=type(policy_error).__name__,
                automatic=True,
                stage="policy",
            )
            raise policy_error from error

        self._events.emit(
            "compaction_evaluated",
            event_context=correlation_context,
            automatic=True,
            should_compact=decision.should_compact,
            estimated_tokens=decision.estimated_tokens,
            token_threshold=decision.token_threshold,
            keep_recent_turns=decision.keep_recent_turns,
            candidate_message_count=len(candidate_messages),
        )
        if not decision.should_compact:
            return

        try:
            preparation = prepare_compaction(
                writer.snapshot.messages,
                keep_recent_turns=decision.keep_recent_turns,
            )
        except CompactionNotNeededError:
            self._events.emit(
                "compaction_skipped",
                event_context=correlation_context,
                automatic=True,
                reason="no_compactable_history",
                source_version=writer.snapshot.version,
            )
            return
        except AgentError as error:
            self._events.emit(
                "compaction_failed",
                event_context=correlation_context,
                error=type(error).__name__,
                automatic=True,
                stage="preparation",
            )
            raise
        except Exception as error:
            wrapped = CompactionError("compaction preparation failed")
            self._events.emit(
                "compaction_failed",
                event_context=correlation_context,
                error=type(wrapped).__name__,
                automatic=True,
                stage="preparation",
            )
            raise wrapped from error

        await self._execute_compaction(
            writer,
            preparation,
            automatic=True,
            request_number=request_number,
            event_context=correlation_context,
        )

    async def _execute_compaction(
        self,
        writer: _SessionWriter,
        preparation: CompactionPreparation,
        *,
        automatic: bool,
        request_number: int,
        event_context: SessionEventContext,
    ) -> SessionSnapshot:
        compaction = self._compaction
        if compaction is None:
            raise ConfigurationError("compaction strategy is not configured")
        strategy = compaction.strategy

        source_version = writer.snapshot.version
        request_context = ProviderRequestContext(
            request_id=uuid4().hex,
            attempt_id=uuid4().hex,
            purpose="compaction",
            request_number=request_number,
            session_id=self._session_id,
            run_id=event_context.run_id,
            turn_id=event_context.turn_id,
        )
        span_id = request_context.request_id
        started_at = time.perf_counter()
        self._events.emit(
            "compaction_started",
            event_context=event_context,
            request_id=request_context.request_id,
            span_id=span_id,
            automatic=automatic,
            source_version=source_version,
            source_message_count=preparation.source_message_count,
            summarized_message_count=len(preparation.messages_to_summarize),
            kept_message_count=len(preparation.kept_messages),
        )
        try:
            summary = await strategy.summarize(
                preparation.messages_to_summarize,
                context=request_context,
            )
            if not isinstance(summary, str):
                raise CompactionError(
                    "compaction strategy must return summary text"
                )

            compacted = await writer.compact(
                SessionCompaction(
                    replace_start=preparation.replace_start,
                    replaced_message_count=len(
                        preparation.messages_to_summarize
                    ),
                    summary_message=build_summary_message(summary),
                ),
                event_context,
            )
        except asyncio.CancelledError:
            self._events.emit(
                "compaction_cancelled",
                event_context=event_context,
                request_id=request_context.request_id,
                span_id=span_id,
                duration_ms=_duration_ms(started_at),
                automatic=automatic,
            )
            raise
        except AgentError as error:
            self._events.emit(
                "compaction_failed",
                event_context=event_context,
                request_id=request_context.request_id,
                span_id=span_id,
                duration_ms=_duration_ms(started_at),
                error=type(error).__name__,
                automatic=automatic,
                stage="execution",
            )
            raise
        except Exception as error:
            wrapped = CompactionError("compaction strategy failed")
            self._events.emit(
                "compaction_failed",
                event_context=event_context,
                request_id=request_context.request_id,
                span_id=span_id,
                duration_ms=_duration_ms(started_at),
                error=type(wrapped).__name__,
                automatic=automatic,
                stage="execution",
            )
            raise wrapped from error

        self._events.emit(
            "compaction_completed",
            event_context=event_context,
            request_id=request_context.request_id,
            span_id=span_id,
            duration_ms=_duration_ms(started_at),
            durable_event_id=_latest_durable_identity(compacted).event_id,
            automatic=automatic,
            source_version=source_version,
            compacted_version=compacted.version,
            compacted_message_count=len(compacted.messages),
        )
        return compacted


class _SessionNextTurnHook:
    """Adapts session compaction to the Agent's generic next-turn hook."""

    def __init__(
        self,
        writer: _SessionWriter,
        compactor: _SessionCompactor,
    ) -> None:
        self._writer = writer
        self._compactor = compactor

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        context: NextTurnContext,
    ) -> Sequence[Message]:
        if tuple(messages) != self._writer.snapshot.messages:
            raise CompactionError(
                "agent and session histories diverged before next-turn preparation"
            )
        await self._compactor.maybe_auto_compact(
            self._writer,
            request_number=context.next_request_number,
            event_context=SessionEventContext(
                run_id=context.run_id,
                turn_id=context.turn_id,
                provenance=EventProvenance(
                    source="compaction",
                    actor_id="automatic-compaction",
                ),
            ),
        )
        return self._writer.snapshot.messages


class _ChainedNextTurnHook:
    """Compose runtime-owned hooks without exposing assembly to Agent."""

    def __init__(self, hooks: tuple[NextTurnHook, ...]) -> None:
        if not hooks:
            raise ValueError("at least one next-turn hook is required")
        self._hooks = hooks

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        context: NextTurnContext,
    ) -> Sequence[Message]:
        prepared = tuple(messages)
        for hook in self._hooks:
            prepared = tuple(await hook.prepare(prepared, context=context))
            if any(not isinstance(message, Message) for message in prepared):
                raise ContextPreparationError(
                    "next-turn hook must return only Message values"
                )
        return prepared
