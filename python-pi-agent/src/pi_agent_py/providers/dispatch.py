"""Bind a run-local dispatch hook without modifying a shared provider instance."""

from collections.abc import AsyncIterator, Callable, Sequence
from typing import cast

from ..domain import Message, ModelStreamEvent, ProviderRequestContext, ToolDefinition
from ..ports import ModelDispatchHook, ModelProvider


def bind_model_dispatch(
    provider: ModelProvider, hook: ModelDispatchHook
) -> ModelProvider:
    binder = getattr(provider, "bind_dispatch", None)
    if callable(binder):
        return cast(Callable[[ModelDispatchHook], ModelProvider], binder)(hook)
    return _DispatchProvider(provider, hook)


class _DispatchProvider:
    def __init__(self, provider: ModelProvider, hook: ModelDispatchHook) -> None:
        self._provider, self._hook = provider, hook

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        await self._hook.before_dispatch(context)
        async for event in self._provider.stream(messages, tools, context=context):
            yield event
