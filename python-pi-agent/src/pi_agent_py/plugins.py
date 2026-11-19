"""Explicit, transactional plugin assembly for tools and runtime hooks."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .domain import (
    ContextPlan,
    EventKind,
    Message,
    NextTurnContext,
    ProviderRequestContext,
)
from .errors import (
    PluginCleanupError,
    PluginExecutionError,
    PluginLoadError,
    PluginRegistrationError,
)
from .observability.events import EventSinkFailure, ensure_isolated_event_sink, make_agent_event
from .ports import EventSink, NextTurnHook, Tool

CleanupCallback = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class SessionPluginContext:
    """Identity available while creating one isolated plugin session."""

    session_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")


class SessionPlugin(Protocol):
    """Per-session interceptor, observer, and resource lifecycle."""

    async def before_provider_request(
        self,
        context: ProviderRequestContext,
        plan: ContextPlan,
    ) -> ContextPlan:
        ...

    async def on_run_settled(self, run_id: str) -> None:
        ...

    async def close(self) -> None:
        ...


class SessionPluginFactory(Protocol):
    def __call__(
        self,
        context: SessionPluginContext,
    ) -> Awaitable[SessionPlugin] | SessionPlugin:
        ...


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

    def register_session_plugin(
        self,
        factory: SessionPluginFactory,
        *,
        priority: int = 0,
        open_timeout_seconds: float | None = None,
        hook_timeout_seconds: float | None = None,
    ) -> Registration:
        """Stage a factory that creates isolated state for each session."""
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
    session_plugin_names: tuple[str, ...]


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
class _SessionFactoryRegistration:
    plugin_name: str
    factory: SessionPluginFactory
    priority: int
    open_timeout_seconds: float | None
    hook_timeout_seconds: float | None
    order: int
    handle: Registration


@dataclass(slots=True)
class _PluginState:
    name: str
    tools: list[_ToolRegistration]
    hooks: list[_HookRegistration]
    cleanups: list[_CleanupRegistration]
    session_factories: list[_SessionFactoryRegistration]


class _StagedPluginAPI:
    """Collects registrations privately until the factory succeeds."""

    def __init__(self, plugin_name: str) -> None:
        self.plugin_name = plugin_name
        self.tools: list[_ToolRegistration] = []
        self.hooks: list[_HookRegistration] = []
        self.cleanups: list[_CleanupRegistration] = []
        self.session_factories: list[_SessionFactoryRegistration] = []

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

    def register_session_plugin(
        self,
        factory: SessionPluginFactory,
        *,
        priority: int = 0,
        open_timeout_seconds: float | None = None,
        hook_timeout_seconds: float | None = None,
    ) -> Registration:
        for name, value in (
            ("open_timeout_seconds", open_timeout_seconds),
            ("hook_timeout_seconds", hook_timeout_seconds),
        ):
            if value is not None and value < 0:
                raise PluginRegistrationError(f"{name} must not be negative")
        handle = Registration(lambda: None)
        item = _SessionFactoryRegistration(
            plugin_name=self.plugin_name,
            factory=factory,
            priority=priority,
            open_timeout_seconds=open_timeout_seconds,
            hook_timeout_seconds=hook_timeout_seconds,
            order=-1,
            handle=handle,
        )
        handle._rebind(lambda: _remove_identity(self.session_factories, item))
        self.session_factories.append(item)
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


@dataclass(frozen=True, slots=True)
class _OpenedSessionPlugin:
    registration: _SessionFactoryRegistration
    plugin: SessionPlugin


class PluginSessionLease:
    """Owns per-session plugin instances and their request interceptor chain."""

    def __init__(
        self,
        host: PluginHost,
        session_id: str,
        plugins: Sequence[_OpenedSessionPlugin],
        event_sink: EventSink,
    ) -> None:
        self._host = host
        self.session_id = session_id
        self._plugins = tuple(plugins)
        self._event_sink = event_sink
        self._closed = False
        self._observer_tasks: set[asyncio.Task[None]] = set()

    @property
    def plugin_names(self) -> tuple[str, ...]:
        return tuple(item.registration.plugin_name for item in self._plugins)

    async def prepare(
        self,
        context: ProviderRequestContext,
        plan: ContextPlan,
    ) -> ContextPlan:
        if self._closed:
            raise PluginExecutionError("plugin session lease is closed")
        current = deepcopy(plan)
        for item in self._plugins:
            registration = item.registration
            span_id = uuid4().hex
            started_at = time.perf_counter()
            self._emit(
                "plugin_hook_started",
                request_context=context,
                span_id=span_id,
                plugin_name=registration.plugin_name,
                hook="before_provider_request",
            )
            try:
                call = item.plugin.before_provider_request(
                    context,
                    deepcopy(current),
                )
                if registration.hook_timeout_seconds is None:
                    prepared = await call
                else:
                    async with asyncio.timeout(
                        registration.hook_timeout_seconds
                    ):
                        prepared = await call
                if not isinstance(prepared, ContextPlan):
                    raise TypeError("session plugin must return ContextPlan")
                if prepared.source_message_count != plan.source_message_count:
                    raise ValueError(
                        "session plugin cannot change source_message_count"
                    )
            except asyncio.CancelledError:
                self._emit(
                    "plugin_hook_cancelled",
                    request_context=context,
                    span_id=span_id,
                    duration_ms=_duration_ms(started_at),
                    plugin_name=registration.plugin_name,
                    hook="before_provider_request",
                )
                raise
            except Exception as error:
                self._emit(
                    "plugin_hook_failed",
                    request_context=context,
                    span_id=span_id,
                    duration_ms=_duration_ms(started_at),
                    plugin_name=registration.plugin_name,
                    hook="before_provider_request",
                    error=type(error).__name__,
                )
                raise PluginExecutionError(
                    "provider request hook from plugin "
                    f'"{registration.plugin_name}" failed'
                ) from error
            current = prepared
            self._emit(
                "plugin_hook_finished",
                request_context=context,
                span_id=span_id,
                duration_ms=_duration_ms(started_at),
                plugin_name=registration.plugin_name,
                hook="before_provider_request",
            )
        return current

    def notify_run_settled(self, run_id: str) -> None:
        """Schedule a best-effort observer without blocking the dispatcher."""
        if self._closed:
            return
        task = asyncio.create_task(
            self._observe_run_settled(run_id),
            name=f"plugin-settled:{self.session_id}:{run_id}",
        )
        self._observer_tasks.add(task)
        task.add_done_callback(self._observer_tasks.discard)

    async def _observe_run_settled(self, run_id: str) -> None:
        for item in self._plugins:
            span_id = uuid4().hex
            started_at = time.perf_counter()
            self._emit(
                "plugin_hook_started",
                run_id=run_id,
                span_id=span_id,
                plugin_name=item.registration.plugin_name,
                hook="on_run_settled",
            )
            try:
                await item.plugin.on_run_settled(run_id)
            except asyncio.CancelledError:
                self._emit(
                    "plugin_hook_cancelled",
                    run_id=run_id,
                    span_id=span_id,
                    duration_ms=_duration_ms(started_at),
                    plugin_name=item.registration.plugin_name,
                    hook="on_run_settled",
                )
                raise
            except Exception as error:
                self._emit(
                    "plugin_hook_failed",
                    run_id=run_id,
                    span_id=span_id,
                    duration_ms=_duration_ms(started_at),
                    plugin_name=item.registration.plugin_name,
                    hook="on_run_settled",
                    error=type(error).__name__,
                )
            else:
                self._emit(
                    "plugin_hook_finished",
                    run_id=run_id,
                    span_id=span_id,
                    duration_ms=_duration_ms(started_at),
                    plugin_name=item.registration.plugin_name,
                    hook="on_run_settled",
                )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._observer_tasks:
            await asyncio.gather(
                *tuple(self._observer_tasks),
                return_exceptions=True,
            )
        errors = await _close_session_plugins(self._plugins)
        await self._host._release_session(self)
        if errors:
            raise PluginCleanupError(
                f"session plugin lease had {len(errors)} cleanup failure(s)"
            ) from errors[0]

    async def __aenter__(self) -> PluginSessionLease:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info
        await self.close()

    def _emit(
        self,
        kind: EventKind,
        *,
        request_context: ProviderRequestContext | None = None,
        run_id: str | None = None,
        span_id: str | None = None,
        duration_ms: float | None = None,
        **payload: object,
    ) -> None:
        context = request_context
        self._event_sink.emit(
            make_agent_event(
                kind,
                "capability",
                source="plugin_session",
                payload=payload,
                session_id=self.session_id,
                run_id=context.run_id if context is not None else run_id,
                turn_id=context.turn_id if context is not None else None,
                request_id=context.request_id if context is not None else None,
                attempt_id=context.attempt_id if context is not None else None,
                span_id=span_id,
                duration_ms=duration_ms,
            )
        )


class PluginHost:
    """Loads in-process plugin factories without import-time side effects."""

    def __init__(self, *, event_sink: EventSink | None = None) -> None:
        self._event_sink = ensure_isolated_event_sink(event_sink)
        self._plugins: dict[str, _PluginState] = {}
        self._tools: list[_ToolRegistration] = []
        self._hooks: list[_HookRegistration] = []
        self._session_factories: list[_SessionFactoryRegistration] = []
        self._session_leases: set[PluginSessionLease] = set()
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
                session_factories=staged.session_factories,
            )
            for item in state.tools:
                item.handle._rebind(lambda item=item: self._remove_tool(item))
                self._tools.append(item)
            for item in state.hooks:
                item.order = self._next_order
                self._next_order += 1
                item.handle._rebind(lambda item=item: self._remove_hook(item))
                self._hooks.append(item)
            for item in state.session_factories:
                item.order = self._next_order
                self._next_order += 1
                item.handle._rebind(
                    lambda item=item: self._remove_session_factory(item)
                )
                self._session_factories.append(item)
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
            session_plugin_names=tuple(
                dict.fromkeys(
                    item.plugin_name
                    for item in self._session_factories
                    if item.handle.active
                )
            ),
        )

    async def open_session(self, session_id: str) -> PluginSessionLease:
        """Create an isolated, stable plugin snapshot for one AgentSession."""
        context = SessionPluginContext(session_id)
        async with self._lock:
            if self._closed:
                raise PluginRegistrationError("plugin host is closed")
            registrations = tuple(
                sorted(
                    (
                        item
                        for item in self._session_factories
                        if item.handle.active
                    ),
                    key=lambda item: (item.priority, item.order),
                )
            )
            opened: list[_OpenedSessionPlugin] = []
            try:
                for item in registrations:
                    call = item.factory(context)
                    if inspect.isawaitable(call):
                        if item.open_timeout_seconds is None:
                            plugin = await call
                        else:
                            async with asyncio.timeout(item.open_timeout_seconds):
                                plugin = await call
                    else:
                        plugin = call
                    _validate_session_plugin(plugin)
                    opened.append(_OpenedSessionPlugin(item, plugin))
            except asyncio.CancelledError:
                await _close_session_plugins(opened)
                raise
            except Exception as error:
                await _close_session_plugins(opened)
                raise PluginExecutionError(
                    f"failed to open plugins for session {session_id!r}"
                ) from error

            lease = PluginSessionLease(
                self,
                session_id,
                opened,
                self._event_sink,
            )
            self._session_leases.add(lease)
            self._emit(
                "plugin_session_opened",
                session_id=session_id,
                plugin_names=lease.plugin_names,
            )
            return lease

    async def unload(self, name: str) -> None:
        """Detach one plugin first, then run every cleanup in reverse order."""
        async with self._lock:
            if any(name in lease.plugin_names for lease in self._session_leases):
                raise PluginRegistrationError(
                    f'plugin "{name}" still has active session leases'
                )
            state = self._plugins.pop(name, None)
            if state is None:
                return
            for item in state.tools:
                item.handle.unregister()
            for item in state.hooks:
                item.handle.unregister()
            for item in state.session_factories:
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
            leases = tuple(self._session_leases)
            names = tuple(reversed(tuple(self._plugins)))

        errors: list[Exception] = []
        for lease in leases:
            try:
                await lease.close()
            except PluginCleanupError as error:
                errors.append(error)
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

    def _remove_session_factory(
        self,
        item: _SessionFactoryRegistration,
    ) -> None:
        _remove_identity(self._session_factories, item)

    async def _release_session(self, lease: PluginSessionLease) -> None:
        async with self._lock:
            self._session_leases.discard(lease)
        self._emit(
            "plugin_session_closed",
            session_id=lease.session_id,
            plugin_names=lease.plugin_names,
        )

    def _emit(
        self,
        kind: EventKind,
        *,
        session_id: str | None = None,
        **payload: object,
    ) -> None:
        self._event_sink.emit(
            make_agent_event(
                kind,
                "capability",
                source="plugin_host",
                payload=payload,
                session_id=session_id,
            )
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
        context: NextTurnContext,
    ) -> Sequence[Message]:
        current = tuple(messages)
        for item in self._hooks:
            span_id = uuid4().hex
            started_at = time.perf_counter()
            self._emit(
                "plugin_hook_started",
                context=context,
                span_id=span_id,
                plugin_name=item.plugin_name,
                hook="next_turn",
                next_request_number=context.next_request_number,
            )
            try:
                call = item.hook.prepare(
                    current,
                    context=context,
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
                    context=context,
                    span_id=span_id,
                    duration_ms=_duration_ms(started_at),
                    plugin_name=item.plugin_name,
                    hook="next_turn",
                )
                raise
            except Exception as error:
                self._emit(
                    "plugin_hook_failed",
                    context=context,
                    span_id=span_id,
                    duration_ms=_duration_ms(started_at),
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
                context=context,
                span_id=span_id,
                duration_ms=_duration_ms(started_at),
                plugin_name=item.plugin_name,
                hook="next_turn",
                next_request_number=context.next_request_number,
            )
        return current

    def _emit(
        self,
        kind: EventKind,
        *,
        context: NextTurnContext,
        span_id: str | None = None,
        duration_ms: float | None = None,
        **payload: object,
    ) -> None:
        self._event_sink.emit(
            make_agent_event(
                kind,
                "capability",
                source="plugin_next_turn_hook",
                payload=payload,
                session_id=context.session_id,
                run_id=context.run_id,
                turn_id=context.turn_id,
                span_id=span_id,
                duration_ms=duration_ms,
            )
        )


def _duration_ms(started_at: float) -> float:
    return max(0.0, (time.perf_counter() - started_at) * 1000)


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


def _validate_session_plugin(plugin: object) -> None:
    for method_name in (
        "before_provider_request",
        "on_run_settled",
        "close",
    ):
        if not callable(getattr(plugin, method_name, None)):
            raise TypeError(
                f"session plugin is missing callable {method_name}()"
            )


async def _close_session_plugins(
    plugins: Sequence[_OpenedSessionPlugin],
) -> list[Exception]:
    errors: list[Exception] = []
    for item in reversed(plugins):
        try:
            await item.plugin.close()
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
