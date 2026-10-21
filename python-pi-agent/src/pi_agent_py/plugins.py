"""Explicit, transactional plugin assembly for tools and runtime hooks."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .domain import AgentEvent, EventKind, Message
from .errors import (
    PluginCleanupError,
    PluginExecutionError,
    PluginLoadError,
    PluginRegistrationError,
)
from .events import EventSinkFailure, ensure_isolated_event_sink
from .ports import EventSink, NextTurnHook, Tool

CleanupCallback = Callable[[], Awaitable[None] | None]


class Registration:
    """An idempotent handle that removes one staged or active registration."""

    def __init__(self, unregister: Callable[[], None]) -> None:
        self._unregister = unregister
        self._active = True

    @property
    def active(self) -> bool:
        """Return whether this registration still contributes behavior."""
        return self._active

    def unregister(self) -> None:
        """Remove the registration once; later calls are no-ops."""
        if not self._active:
            return
        self._active = False
        self._unregister()

    def _rebind(self, unregister: Callable[[], None]) -> None:
        self._unregister = unregister

    def _deactivate(self) -> None:
        self._active = False


class PluginAPI(Protocol):
    """Capabilities a plugin factory may register during explicit setup."""

    def register_tool(self, tool: Tool) -> Registration:
        """Stage one model-callable tool."""
        ...

    def register_next_turn_hook(
        self,
        hook: NextTurnHook,
        *,
        priority: int = 0,
        timeout_seconds: float | None = None,
    ) -> Registration:
        """Stage an ordered, awaited interceptor for the next-turn boundary."""
        ...

    def register_cleanup(self, callback: CleanupCallback) -> Registration:
        """Register resource cleanup, executed in reverse order."""
        ...


class PluginFactory(Protocol):
    """Initializes one plugin through the restricted registration API."""

    def __call__(self, api: PluginAPI) -> Awaitable[None] | None:
        ...


@dataclass(frozen=True, slots=True)
class PluginCapabilities:
    """Immutable capability snapshot used for explicit runtime assembly."""

    plugin_names: tuple[str, ...]
    tools: tuple[Tool, ...]
    next_turn_hook: NextTurnHook | None


@dataclass(slots=True)
class _ToolRegistration:
    plugin_name: str
    tool: Tool
    handle: Registration


@dataclass(slots=True)
class _HookRegistration:
    plugin_name: str
    hook: NextTurnHook
    priority: int
    timeout_seconds: float | None
    order: int
    handle: Registration


@dataclass(slots=True)
class _CleanupRegistration:
    callback: CleanupCallback
    handle: Registration


@dataclass(slots=True)
class _PluginState:
    name: str
    tools: list[_ToolRegistration]
    hooks: list[_HookRegistration]
    cleanups: list[_CleanupRegistration]


class _StagedPluginAPI:
    """Collects registrations privately until the factory succeeds."""

    def __init__(self, plugin_name: str) -> None:
        self.plugin_name = plugin_name
        self.tools: list[_ToolRegistration] = []
        self.hooks: list[_HookRegistration] = []
        self.cleanups: list[_CleanupRegistration] = []

    def register_tool(self, tool: Tool) -> Registration:
        name = tool.definition.name
        if any(item.tool.definition.name == name for item in self.tools):
            raise PluginRegistrationError(
                f'plugin "{self.plugin_name}" registered duplicate tool "{name}"'
            )

        handle = Registration(lambda: None)
        item = _ToolRegistration(self.plugin_name, tool, handle)
        handle._rebind(lambda: _remove_identity(self.tools, item))
        self.tools.append(item)
        return handle

    def register_next_turn_hook(
        self,
        hook: NextTurnHook,
        *,
        priority: int = 0,
        timeout_seconds: float | None = None,
    ) -> Registration:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise PluginRegistrationError(
                "hook timeout_seconds must not be negative"
            )

        handle = Registration(lambda: None)
        item = _HookRegistration(
            plugin_name=self.plugin_name,
            hook=hook,
            priority=priority,
            timeout_seconds=timeout_seconds,
            order=-1,
            handle=handle,
        )
        handle._rebind(lambda: _remove_identity(self.hooks, item))
        self.hooks.append(item)
        return handle

    def register_cleanup(self, callback: CleanupCallback) -> Registration:
        handle = Registration(lambda: None)
        item = _CleanupRegistration(callback, handle)
        handle._rebind(lambda: _remove_identity(self.cleanups, item))
        self.cleanups.append(item)
        return handle


class PluginHandle:
    """Owns one loaded plugin and can release it exactly once."""

    def __init__(self, host: PluginHost, name: str) -> None:
        self._host = host
        self.name = name

    @property
    def active(self) -> bool:
        """Return whether the plugin is still loaded in its host."""
        return self._host.has_plugin(self.name)

    async def close(self) -> None:
        """Unregister capabilities and release resources."""
        await self._host.unload(self.name)

    async def __aenter__(self) -> PluginHandle:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info
        await self.close()


class PluginHost:
    """Loads in-process plugin factories without import-time side effects."""

    def __init__(self, *, event_sink: EventSink | None = None) -> None:
        self._event_sink = ensure_isolated_event_sink(event_sink)
        self._plugins: dict[str, _PluginState] = {}
        self._tools: list[_ToolRegistration] = []
        self._hooks: list[_HookRegistration] = []
        self._next_order = 0
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def event_sink_failures(self) -> tuple[EventSinkFailure, ...]:
        """Return observer failures that were isolated from plugin execution."""
        return self._event_sink.failures

    def has_plugin(self, name: str) -> bool:
        """Return whether a named plugin is currently active."""
        return name in self._plugins

    async def load(self, name: str, factory: PluginFactory) -> PluginHandle:
        """Run setup transactionally, then publish all registrations together."""
        if not name.strip():
            raise PluginRegistrationError("plugin name must not be empty")

        async with self._lock:
            if self._closed:
                raise PluginRegistrationError("plugin host is closed")
            if name in self._plugins:
                raise PluginRegistrationError(f'plugin "{name}" is already loaded')

            staged = _StagedPluginAPI(name)
            try:
                result = factory(staged)
                if inspect.isawaitable(result):
                    await result
                elif result is not None:
                    raise PluginRegistrationError(
                        "plugin factory must return None or an awaitable"
                    )
                self._validate_tool_conflicts(staged.tools)
            except asyncio.CancelledError:
                await _run_cleanups(staged.cleanups)
                raise
            except Exception as error:
                cleanup_errors = await _run_cleanups(staged.cleanups)
                detail = (
                    f"; {len(cleanup_errors)} rollback cleanup(s) also failed"
                    if cleanup_errors
                    else ""
                )
                raise PluginLoadError(
                    f'plugin "{name}" failed to load{detail}'
                ) from error

            state = _PluginState(
                name=name,
                tools=staged.tools,
                hooks=staged.hooks,
                cleanups=staged.cleanups,
            )
            for item in state.tools:
                item.handle._rebind(lambda item=item: self._remove_tool(item))
                self._tools.append(item)
            for item in state.hooks:
                item.order = self._next_order
                self._next_order += 1
                item.handle._rebind(lambda item=item: self._remove_hook(item))
                self._hooks.append(item)
            self._plugins[name] = state
            self._emit("plugin_loaded", plugin_name=name)
            return PluginHandle(self, name)

    def snapshot(self) -> PluginCapabilities:
        """Freeze active registrations for one explicit runtime assembly."""
        hooks = tuple(
            sorted(
                (item for item in self._hooks if item.handle.active),
                key=lambda item: (item.priority, item.order),
            )
        )
        return PluginCapabilities(
            plugin_names=tuple(self._plugins),
            tools=tuple(
                item.tool for item in self._tools if item.handle.active
            ),
            next_turn_hook=(
                _PluginNextTurnHook(hooks, self._event_sink) if hooks else None
            ),
        )

    async def unload(self, name: str) -> None:
        """Detach one plugin first, then run every cleanup in reverse order."""
        async with self._lock:
            state = self._plugins.pop(name, None)
            if state is None:
                return
            for item in state.tools:
                item.handle.unregister()
            for item in state.hooks:
                item.handle.unregister()

        cleanup_errors = await _run_cleanups(state.cleanups)
        self._emit(
            "plugin_unloaded",
            plugin_name=name,
            cleanup_error_count=len(cleanup_errors),
        )
        if cleanup_errors:
            raise PluginCleanupError(
                f'plugin "{name}" had {len(cleanup_errors)} cleanup failure(s)'
            ) from cleanup_errors[0]

    async def close(self) -> None:
        """Unload plugins in reverse load order and attempt every cleanup."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            names = tuple(reversed(tuple(self._plugins)))

        errors: list[Exception] = []
        for name in names:
            try:
                await self.unload(name)
            except PluginCleanupError as error:
                errors.append(error)
        if errors:
            raise PluginCleanupError(
                f"plugin host had {len(errors)} plugin cleanup failure(s)"
            ) from errors[0]

    def _validate_tool_conflicts(
        self,
        staged_tools: Sequence[_ToolRegistration],
    ) -> None:
        owners = {
            item.tool.definition.name: item.plugin_name
            for item in self._tools
            if item.handle.active
        }
        for item in staged_tools:
            tool_name = item.tool.definition.name
            owner = owners.get(tool_name)
            if owner is not None:
                raise PluginRegistrationError(
                    f'tool "{tool_name}" from plugin "{item.plugin_name}" '
                    f'conflicts with plugin "{owner}"'
                )

    def _remove_tool(self, item: _ToolRegistration) -> None:
        _remove_identity(self._tools, item)

    def _remove_hook(self, item: _HookRegistration) -> None:
        _remove_identity(self._hooks, item)

    def _emit(self, kind: EventKind, **payload: object) -> None:
        self._event_sink.emit(
            AgentEvent(kind=kind, channel="capability", payload=payload)
        )


class _PluginNextTurnHook:
    """Runs a stable hook snapshot in priority and registration order."""

    def __init__(
        self,
        hooks: Sequence[_HookRegistration],
        event_sink: EventSink,
    ) -> None:
        self._hooks = tuple(hooks)
        self._event_sink = event_sink

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        next_request_number: int,
    ) -> Sequence[Message]:
        current = tuple(messages)
        for item in self._hooks:
            self._emit(
                "plugin_hook_started",
                plugin_name=item.plugin_name,
                hook="next_turn",
                next_request_number=next_request_number,
            )
            try:
                call = item.hook.prepare(
                    current,
                    next_request_number=next_request_number,
                )
                if item.timeout_seconds is None:
                    prepared = tuple(await call)
                else:
                    async with asyncio.timeout(item.timeout_seconds):
                        prepared = tuple(await call)
                if any(not isinstance(message, Message) for message in prepared):
                    raise TypeError("hook returned a non-Message value")
            except asyncio.CancelledError:
                self._emit(
                    "plugin_hook_cancelled",
                    plugin_name=item.plugin_name,
                    hook="next_turn",
                )
                raise
            except Exception as error:
                self._emit(
                    "plugin_hook_failed",
                    plugin_name=item.plugin_name,
                    hook="next_turn",
                    error=type(error).__name__,
                )
                raise PluginExecutionError(
                    f'next-turn hook from plugin "{item.plugin_name}" failed'
                ) from error

            current = prepared
            self._emit(
                "plugin_hook_finished",
                plugin_name=item.plugin_name,
                hook="next_turn",
                next_request_number=next_request_number,
            )
        return current

    def _emit(self, kind: EventKind, **payload: object) -> None:
        self._event_sink.emit(
            AgentEvent(kind=kind, channel="capability", payload=payload)
        )


async def _run_cleanups(
    cleanups: Sequence[_CleanupRegistration],
) -> list[Exception]:
    errors: list[Exception] = []
    for item in reversed(cleanups):
        if not item.handle.active:
            continue
        item.handle._deactivate()
        try:
            result = item.callback()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            errors.append(error)
    return errors


def _remove_identity(items: list[object], target: object) -> None:
    for index, item in enumerate(items):
        if item is target:
            items.pop(index)
            return
