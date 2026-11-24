"""Persist normalized model inputs before dispatching to a borrowed provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from copy import deepcopy

from ..domain import (
    Message, ModelRequestTarget, ModelStreamEvent, ProviderRequestContext,
    RecordedModelRequest, ToolDefinition,
)
from ..errors import AgentError, SessionStorageError
from ..ports import ModelProvider, ModelRequestStore, ModelDispatchHook


class RecordingModelProvider:
    """Record each invocation after request hooks and before provider execution.

    The provider and store are borrowed. Their creator closes them. Wrap each
    raw retry candidate to record physical attempts, or wrap the composed
    provider to record logical requests. Target parameters are explicitly
    supplied public settings; credentials and HTTP configuration are not read.
    """

    def __init__(
        self, provider: ModelProvider, store: ModelRequestStore,
        target: ModelRequestTarget,
    ) -> None:
        self._provider = provider
        self._store = store
        self._target = target

    def bind_dispatch(self, hook: "ModelDispatchHook") -> "RecordingModelProvider":
        from .dispatch import bind_model_dispatch
        return RecordingModelProvider(bind_model_dispatch(self._provider, hook), self._store, self._target)

    async def stream(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition] = (),
        *, context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        """Commit detached inputs; storage failure or cancellation prevents dispatch."""
        request = RecordedModelRequest(
            context, self._target, tuple(messages), tuple(tools),
        )
        try:
            await self._store.record(deepcopy(request))
        except AgentError:
            raise
        except Exception as error:
            raise SessionStorageError("model request persistence failed") from error
        # Neither the observer store nor the provider can mutate the other's copy.
        async for event in self._provider.stream(
            deepcopy(request.messages), deepcopy(request.tools), context=request.context,
        ):
            yield event
