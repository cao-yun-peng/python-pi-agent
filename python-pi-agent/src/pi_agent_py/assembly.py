"""Transactional application assembly above provider-neutral runtime ports."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeVar, cast

from .app_runtime import AgentRuntime, SessionPoolPolicy
from .configuration import (
    LoadedAppConfig,
    ProviderResilienceSettings,
    ProviderRuntimeConfig,
)
from .domain import Message, NextTurnContext
from .errors import ConfigurationError, RuntimeBuildError, RuntimeCloseError
from .events import ensure_isolated_event_sink
from .lifecycle import Cleanup, LifecycleScope, ManagedResource, ResourceOwnership
from .model_runtime import (
    DefaultRetryPolicy,
    DirectModelRuntime,
    FullJitterBackoff,
    ProviderCandidate,
    ResilientModelRuntime,
)
from .plugins import PluginFactory, PluginHost
from .ports import (
    ApprovalPolicy,
    CompactionPolicy,
    CompactionStrategy,
    ContextStrategy,
    EventSink,
    ModelProvider,
    NextTurnHook,
    SessionStore,
    Tool,
    ToolExecutionGate,
)
from .profiles import AgentProfile, minimal_profile
from .providers import (
    DeepSeekConfig,
    DeepSeekProvider,
    GLMConfig,
    GLMProvider,
    KimiConfig,
    KimiProvider,
    TimiccResponsesConfig,
    TimiccResponsesProvider,
)
from .session import InMemorySessionStore

T = TypeVar("T")
ProviderFactory = Callable[
    [ProviderRuntimeConfig],
    ModelProvider | Awaitable[ModelProvider],
]
_UNSET = object()


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """One explicit provider factory plus routing metadata."""

    name: str
    factory: ProviderFactory
    supports_tools: bool = True
    state_owners: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider registration name must not be empty")


@dataclass(frozen=True, slots=True)
class BuiltProvider:
    name: str
    provider: ModelProvider
    supports_tools: bool
    state_owners: frozenset[str]


class ProviderCatalog:
    """Immutable, explicitly supplied provider factory catalog."""

    def __init__(self, registrations: Sequence[ProviderRegistration]) -> None:
        values = tuple(registrations)
        names = [item.name for item in values]
        if len(set(names)) != len(names):
            raise ValueError("provider registration names must be unique")
        self._registrations: Mapping[str, ProviderRegistration] = MappingProxyType(
            {item.name: item for item in values}
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._registrations)

    async def build(
        self,
        name: str,
        config: ProviderRuntimeConfig,
    ) -> BuiltProvider:
        registration = self._registrations.get(name)
        if registration is None:
            raise ConfigurationError(
                f'provider "{name}" is not registered in this application'
            )
        value = registration.factory(config)
        provider = await value if inspect.isawaitable(value) else value
        if not callable(getattr(provider, "stream", None)):
            raise TypeError("provider factory must return a ModelProvider")
        return BuiltProvider(
            name=name,
            provider=provider,
            supports_tools=registration.supports_tools,
            state_owners=registration.state_owners,
        )


def default_provider_catalog() -> ProviderCatalog:
    """Return built-in registrations without mutating process-global state."""
    return ProviderCatalog(
        (
            ProviderRegistration(
                "deepseek",
                lambda config: DeepSeekProvider(
                    _expect_config(config, DeepSeekConfig, "deepseek")
                ),
            ),
            ProviderRegistration(
                "kimi",
                lambda config: KimiProvider(
                    _expect_config(config, KimiConfig, "kimi")
                ),
            ),
            ProviderRegistration(
                "glm",
                lambda config: GLMProvider(
                    _expect_config(config, GLMConfig, "glm")
                ),
            ),
            ProviderRegistration(
                "timicc",
                lambda config: TimiccResponsesProvider(
                    _expect_config(config, TimiccResponsesConfig, "timicc")
                ),
                state_owners=frozenset({"timicc-responses"}),
            ),
        )
    )


class AgentRuntimeBuilder:
    """Single-use builder that either publishes a complete runtime or rolls back."""

    def __init__(self) -> None:
        self._loaded_config: LoadedAppConfig | None = None
        self._provider_catalog: ProviderCatalog | None = None
        self._provider: ManagedResource[ModelProvider] | None = None
        self._provider_name = "direct"
        self._provider_supports_tools = True
        self._provider_state_owners: frozenset[str] = frozenset()
        self._resilience = ProviderResilienceSettings()
        self._session_store: ManagedResource[SessionStore] | None = None
        self._event_sink: ManagedResource[EventSink] | None = None
        self._profile = minimal_profile()
        self._plugins: list[tuple[str, PluginFactory]] = []
        self._tools: tuple[Tool, ...] | None = None
        self._approval_policy: ApprovalPolicy | None = None
        self._execution_gate: ToolExecutionGate | None = None
        self._context_strategy: ContextStrategy | None = None
        self._compaction_policy: CompactionPolicy | None = None
        self._compaction_strategy: CompactionStrategy | None = None
        self._next_turn_hook: NextTurnHook | None = None
        self._system_prompt: str | None | object = _UNSET
        self._max_model_requests: int | None = None
        self._session_pool_policy = SessionPoolPolicy()
        self._graceful_shutdown_seconds = 5.0
        self._used = False

    @classmethod
    def from_loaded_config(
        cls,
        config: LoadedAppConfig,
        *,
        provider_catalog: ProviderCatalog,
    ) -> AgentRuntimeBuilder:
        builder = cls()
        builder._loaded_config = config
        builder._provider_catalog = provider_catalog
        builder._resilience = config.spec.resilience
        return builder

    def with_provider(
        self,
        provider: ModelProvider,
        *,
        name: str = "direct",
        ownership: ResourceOwnership = "borrowed",
        cleanup: Cleanup | None = None,
        supports_tools: bool = True,
        state_owners: frozenset[str] = frozenset(),
    ) -> AgentRuntimeBuilder:
        if not name.strip():
            raise ValueError("provider name must not be empty")
        self._provider = ManagedResource(
            name=f"provider:{name}",
            value=provider,
            ownership=ownership,
            cleanup=cleanup,
        )
        self._provider_name = name
        self._provider_supports_tools = supports_tools
        self._provider_state_owners = state_owners
        self._loaded_config = None
        self._provider_catalog = None
        return self

    def with_resilience(
        self,
        settings: ProviderResilienceSettings,
    ) -> AgentRuntimeBuilder:
        self._resilience = settings
        return self

    def with_session_store(
        self,
        store: SessionStore,
        *,
        ownership: ResourceOwnership = "borrowed",
        cleanup: Cleanup | None = None,
    ) -> AgentRuntimeBuilder:
        self._session_store = ManagedResource(
            name="session-store",
            value=store,
            ownership=ownership,
            cleanup=cleanup,
        )
        return self

    def with_event_sink(
        self,
        sink: EventSink,
        *,
        ownership: ResourceOwnership = "borrowed",
        cleanup: Cleanup | None = None,
    ) -> AgentRuntimeBuilder:
        self._event_sink = ManagedResource(
            name="event-sink",
            value=sink,
            ownership=ownership,
            cleanup=cleanup,
        )
        return self

    def with_profile(self, profile: AgentProfile) -> AgentRuntimeBuilder:
        self._profile = profile
        return self

    def with_plugin(
        self,
        name: str,
        factory: PluginFactory,
    ) -> AgentRuntimeBuilder:
        if not name.strip():
            raise ValueError("plugin name must not be empty")
        self._plugins.append((name, factory))
        return self

    def with_tools(self, tools: Sequence[Tool]) -> AgentRuntimeBuilder:
        self._tools = tuple(tools)
        return self

    def with_approval_policy(
        self,
        policy: ApprovalPolicy,
    ) -> AgentRuntimeBuilder:
        self._approval_policy = policy
        return self

    def with_execution_gate(
        self,
        gate: ToolExecutionGate,
    ) -> AgentRuntimeBuilder:
        self._execution_gate = gate
        return self

    def with_context_strategy(
        self,
        strategy: ContextStrategy,
    ) -> AgentRuntimeBuilder:
        self._context_strategy = strategy
        return self

    def with_compaction(
        self,
        *,
        policy: CompactionPolicy | None,
        strategy: CompactionStrategy | None,
    ) -> AgentRuntimeBuilder:
        self._compaction_policy = policy
        self._compaction_strategy = strategy
        return self

    def with_next_turn_hook(self, hook: NextTurnHook) -> AgentRuntimeBuilder:
        self._next_turn_hook = hook
        return self

    def with_system_prompt(self, prompt: str | None) -> AgentRuntimeBuilder:
        if prompt is not None and not prompt.strip():
            raise ValueError("system prompt must not be empty")
        self._system_prompt = prompt
        return self

    def with_max_model_requests(self, value: int) -> AgentRuntimeBuilder:
        if type(value) is not int or value <= 0:
            raise ValueError("max_model_requests must be a positive integer")
        self._max_model_requests = value
        return self

    def with_session_pool(
        self,
        policy: SessionPoolPolicy,
    ) -> AgentRuntimeBuilder:
        self._session_pool_policy = policy
        return self

    def with_graceful_shutdown(self, seconds: float) -> AgentRuntimeBuilder:
        self._graceful_shutdown_seconds = seconds
        return self

    async def build(self) -> AgentRuntime:
        if self._used:
            raise RuntimeBuildError("agent runtime builder can only be used once")
        self._used = True
        lifecycle = LifecycleScope(name="runtime-components")
        observer_lifecycle = LifecycleScope(name="runtime-observers")
        try:
            raw_sink = (
                observer_lifecycle.manage(self._event_sink)
                if self._event_sink is not None
                else None
            )
            event_sink = ensure_isolated_event_sink(raw_sink)
            store = lifecycle.manage(
                self._session_store
                or ManagedResource(
                    name="session-store",
                    value=InMemorySessionStore(),
                    ownership="owned",
                )
            )
            built_provider = await self._build_provider()
            raw_provider = lifecycle.manage(
                ManagedResource(
                    name=f"provider:{built_provider.name}",
                    value=built_provider.provider,
                    ownership=(
                        self._provider.ownership
                        if self._provider is not None
                        else "owned"
                    ),
                    cleanup=(
                        self._provider.cleanup
                        if self._provider is not None
                        else None
                    ),
                )
            )
            provider = self._compose_model_runtime(
                built_provider,
                raw_provider,
                event_sink,
            )

            profile = self._profile
            tools = (
                self._tools
                if self._tools is not None
                else tuple(factory() for factory in profile.tool_factories)
            )
            approval_policy = _resolve_override(
                self._approval_policy,
                profile.approval_policy_factory,
            )
            execution_gate = _resolve_override(
                self._execution_gate,
                profile.execution_gate_factory,
            )
            context_strategy = _resolve_override(
                self._context_strategy,
                profile.context_strategy_factory,
            )
            compaction_policy = _resolve_override(
                self._compaction_policy,
                profile.compaction_policy_factory,
            )
            compaction_strategy = _resolve_override(
                self._compaction_strategy,
                profile.compaction_strategy_factory,
            )
            profile_hook = _resolve_override(
                self._next_turn_hook,
                profile.next_turn_hook_factory,
            )
            if compaction_policy is not None and compaction_strategy is None:
                raise ConfigurationError(
                    "compaction policy requires a compaction strategy"
                )

            plugin_host: PluginHost | None = None
            session_plugin_host: PluginHost | None = None
            plugin_hook: NextTurnHook | None = None
            if self._plugins:
                plugin_host = lifecycle.manage(
                    ManagedResource(
                        name="plugin-host",
                        value=PluginHost(event_sink=event_sink),
                        ownership="owned",
                    )
                )
                for name, factory in self._plugins:
                    await plugin_host.load(name, factory)
                capabilities = plugin_host.snapshot()
                tools = tuple(tools) + capabilities.tools
                plugin_hook = capabilities.next_turn_hook
                if capabilities.session_plugin_names:
                    session_plugin_host = plugin_host

            _validate_tool_names(tools)
            next_turn_hook = _combine_hooks(profile_hook, plugin_hook)
            system_prompt = (
                profile.system_prompt
                if self._system_prompt is _UNSET
                else cast(str | None, self._system_prompt)
            )
            max_model_requests = (
                profile.max_model_requests
                if self._max_model_requests is None
                else self._max_model_requests
            )
            return AgentRuntime(
                provider,
                store,
                event_sink=event_sink,
                tools=tools,
                approval_policy=approval_policy,
                execution_gate=execution_gate,
                compaction_policy=compaction_policy,
                compaction_strategy=compaction_strategy,
                context_strategy=context_strategy,
                system_prompt=system_prompt,
                max_model_requests=max_model_requests,
                plugin_host=session_plugin_host,
                next_turn_hook=next_turn_hook,
                session_pool_policy=self._session_pool_policy,
                graceful_shutdown_seconds=self._graceful_shutdown_seconds,
                lifecycle=lifecycle,
                observer_lifecycle=observer_lifecycle,
            )
        except asyncio.CancelledError:
            await _rollback(lifecycle, observer_lifecycle)
            raise
        except BaseException as error:
            cleanup_failures = await _rollback(lifecycle, observer_lifecycle)
            detail = (
                f"; {len(cleanup_failures)} rollback cleanup(s) also failed"
                if cleanup_failures
                else ""
            )
            raise RuntimeBuildError(
                f"agent runtime assembly failed{detail}"
            ) from error

    async def _build_provider(self) -> BuiltProvider:
        if self._provider is not None:
            return BuiltProvider(
                name=self._provider_name,
                provider=self._provider.value,
                supports_tools=self._provider_supports_tools,
                state_owners=self._provider_state_owners,
            )
        if self._loaded_config is None or self._provider_catalog is None:
            raise ConfigurationError(
                "agent runtime builder requires an explicit provider or loaded config"
            )
        return await self._provider_catalog.build(
            self._loaded_config.provider_name,
            self._loaded_config.provider_config,
        )

    def _compose_model_runtime(
        self,
        built: BuiltProvider,
        raw_provider: ModelProvider,
        event_sink: EventSink,
    ) -> ModelProvider:
        settings = self._resilience
        if not settings.enabled:
            return DirectModelRuntime(
                raw_provider,
                name=built.name,
                event_sink=event_sink,
            )
        return ResilientModelRuntime(
            (
                ProviderCandidate(
                    built.name,
                    raw_provider,
                    supports_tools=built.supports_tools,
                    state_owners=built.state_owners,
                ),
            ),
            retry_policy=DefaultRetryPolicy(
                max_attempts_per_provider=settings.max_attempts_per_provider
            ),
            backoff=FullJitterBackoff(
                base_delay_seconds=settings.base_delay_seconds,
                max_delay_seconds=settings.max_delay_seconds,
            ),
            event_sink=event_sink,
            max_total_attempts=settings.max_total_attempts,
            max_elapsed_seconds=settings.max_elapsed_seconds,
        )


class _CombinedNextTurnHook:
    def __init__(self, hooks: tuple[NextTurnHook, ...]) -> None:
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
        return prepared


def _combine_hooks(*hooks: NextTurnHook | None) -> NextTurnHook | None:
    selected = tuple(hook for hook in hooks if hook is not None)
    if not selected:
        return None
    if len(selected) == 1:
        return selected[0]
    return _CombinedNextTurnHook(selected)


def _build_optional(factory: Callable[[], T] | None) -> T | None:
    return factory() if factory is not None else None


def _resolve_override(
    override: T | None,
    factory: Callable[[], T] | None,
) -> T | None:
    if override is not None:
        return override
    return _build_optional(factory)


def _validate_tool_names(tools: Sequence[Tool]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for tool in tools:
        name = tool.definition.name
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise ConfigurationError(
            "duplicate assembled tool names: " + ", ".join(sorted(duplicates))
        )


async def _rollback(*scopes: LifecycleScope) -> tuple[BaseException, ...]:
    failures: list[BaseException] = []
    for scope in scopes:
        try:
            await scope.aclose()
        except RuntimeCloseError as error:
            failures.extend(error.failures)
        except BaseException as error:
            failures.append(error)
    return tuple(failures)


def _expect_config(
    config: ProviderRuntimeConfig,
    expected: type[T],
    provider_name: str,
) -> T:
    if not isinstance(config, expected):
        raise ConfigurationError(
            f'provider "{provider_name}" received incompatible configuration'
        )
    return config
