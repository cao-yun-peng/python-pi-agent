"""Transactional application assembly above provider-neutral runtime ports."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TypeVar, cast

from ._observation import _RuntimeObservation
from .runtime import AgentRuntime, SessionPoolPolicy
from ..runtime.components import AgentComponents
from ..runtime.execution_config import DistributedExecution, DistributedExecutionSettings
from ..runtime.recovery import SessionRecoveryWorker, RecoveryWorkerSettings
from ..ports import RecoveryDispatcher
from ..ports import SessionExecutionStore, SessionLeaseManager, LeaseClock
from ..runtime.session_spec import SessionSpecResolver
from ..runtime.middleware import Middleware
from .capability_catalog import CapabilityCatalog, default_capability_catalog
from .configuration import (
    LoadedAppConfig,
    ProviderResilienceSettings,
)
from ..domain import Message, NextTurnContext
from ..errors import ConfigurationError, RuntimeBuildError, RuntimeCloseError
from .lifecycle import Cleanup, LifecycleScope, ManagedResource, ResourceOwnership
from ..providers.runtime import (
    DefaultRetryPolicy,
    DirectModelRuntime,
    FullJitterBackoff,
    ResilientModelRuntime,
)
from ..plugins import PluginFactory, PluginHost
from ..ports import (
    ApprovalPolicy,
    CompactionPolicy,
    CompactionStrategy,
    ContextStrategy,
    EventSink,
    ModelProvider,
    ProviderCandidate,
    NextTurnHook,
    SchemaValidator,
    SessionStore,
    TelemetrySink,
    Tool,
    ToolExecutionGate,
)
from .profiles import AgentProfile, minimal_profile
from .provider_catalog import (
    BuiltProvider as BuiltProvider,
    ProviderCatalog as ProviderCatalog,
    ProviderFactory as ProviderFactory,
    ProviderRegistration as ProviderRegistration,
    default_provider_catalog as default_provider_catalog,
)
from ..storage.memory import InMemorySessionStore
from ..observability.usage import PricingTable

T = TypeVar("T")
_UNSET = object()


class AgentRuntimeBuilder:
    """Single-use builder that either publishes a complete runtime or rolls back."""

    def __init__(self) -> None:
        self._loaded_config: LoadedAppConfig | None = None
        self._provider_catalog: ProviderCatalog | None = None
        self._capability_catalog: CapabilityCatalog | None = None
        self._provider: ManagedResource[ModelProvider] | None = None
        self._provider_name = "direct"
        self._provider_model: str | None = None
        self._provider_supports_tools = True
        self._provider_state_owners: frozenset[str] = frozenset()
        self._resilience = ProviderResilienceSettings()
        self._session_store: ManagedResource[SessionStore] | None = None
        self._distributed_execution: DistributedExecution | None = None
        self._execution_resource: ManagedResource[SessionExecutionStore] | None = None
        self._lease_resource: ManagedResource[SessionLeaseManager] | None = None
        self._recovery_resource: ManagedResource[RecoveryDispatcher] | None = None
        self._recovery_settings = RecoveryWorkerSettings()
        self._session_spec_resolver: ManagedResource[SessionSpecResolver] | None = None
        self._session_spec_resolver_factory: (
            Callable[[AgentComponents], SessionSpecResolver] | None
        ) = None
        self._event_sink: ManagedResource[EventSink] | None = None
        self._telemetry_sink: ManagedResource[TelemetrySink] | None = None
        self._pricing: PricingTable | None = None
        self._profile = minimal_profile()
        self._plugins: list[tuple[str, PluginFactory]] = []
        self._tools: tuple[Tool, ...] | None = None
        self._middleware: tuple[Middleware, ...] | None = None
        self._approval_policy: ApprovalPolicy | None = None
        self._execution_gate: ToolExecutionGate | None = None
        self._context_strategy: ContextStrategy | None = None
        self._schema_validator: SchemaValidator | None = None
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
        capability_catalog: CapabilityCatalog | None = None,
    ) -> AgentRuntimeBuilder:
        builder = cls()
        builder._loaded_config = config
        builder._provider_catalog = provider_catalog
        builder._capability_catalog = capability_catalog
        builder._resilience = config.spec.resilience
        return builder

    def with_provider(
        self,
        provider: ModelProvider,
        *,
        name: str = "direct",
        model: str | None = None,
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
        self._provider_model = model
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

    def with_distributed_execution(
        self, store: SessionExecutionStore, leases: SessionLeaseManager, *,
        settings: DistributedExecutionSettings, clock: LeaseClock | None = None,
        store_ownership: ResourceOwnership = "borrowed", store_cleanup: Cleanup | None = None,
        lease_ownership: ResourceOwnership = "borrowed", lease_cleanup: Cleanup | None = None,
    ) -> AgentRuntimeBuilder:
        self._execution_resource = ManagedResource("execution-store", store, store_ownership, store_cleanup)
        self._lease_resource = ManagedResource("lease-manager", leases, lease_ownership, lease_cleanup)
        self._distributed_execution = DistributedExecution(store, leases, settings, clock)
        return self

    def with_session_spec_resolver(
        self,
        resolver: SessionSpecResolver,
        *,
        ownership: ResourceOwnership = "borrowed",
        cleanup: Cleanup | None = None,
    ) -> AgentRuntimeBuilder:
        self._session_spec_resolver = ManagedResource(
            "session-spec-resolver", resolver, ownership, cleanup
        )
        self._session_spec_resolver_factory = None
        return self

    def with_recovery(
        self,
        dispatcher: RecoveryDispatcher,
        *,
        settings: RecoveryWorkerSettings = RecoveryWorkerSettings(),
        ownership: ResourceOwnership = "borrowed",
        cleanup: Cleanup | None = None,
    ) -> AgentRuntimeBuilder:
        self._recovery_resource = ManagedResource(
            "recovery-dispatcher", dispatcher, ownership, cleanup
        )
        self._recovery_settings = settings
        return self

    def with_session_spec_resolver_factory(
        self, factory: Callable[[AgentComponents], SessionSpecResolver]
    ) -> AgentRuntimeBuilder:
        """Build an owned resolver from the fully assembled default components."""
        self._session_spec_resolver_factory = factory
        self._session_spec_resolver = None
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

    def with_telemetry_sink(
        self,
        sink: TelemetrySink,
        *,
        ownership: ResourceOwnership = "borrowed",
        cleanup: Cleanup | None = None,
    ) -> AgentRuntimeBuilder:
        self._telemetry_sink = ManagedResource("telemetry-sink", sink, ownership, cleanup)
        return self

    def with_pricing(self, pricing: PricingTable) -> AgentRuntimeBuilder:
        self._pricing = pricing
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

    def with_middleware(self, middleware: Sequence[Middleware]) -> AgentRuntimeBuilder:
        """Replace Profile middleware; plugin middleware is appended afterwards."""
        self._middleware = tuple(middleware)
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

    def with_schema_validator(self, validator: SchemaValidator) -> AgentRuntimeBuilder:
        """Replace the Profile's argument validator before component assembly."""
        self._schema_validator = validator
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
        """Replace the complete prompt (None disables profile text and tool rules)."""
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
            telemetry = (
                observer_lifecycle.manage(self._telemetry_sink)
                if self._telemetry_sink is not None else None
            )
            observation = _RuntimeObservation(
                raw_sink, telemetry, self._pricing,
                self._loaded_config.provider_config.model
                if self._loaded_config is not None else self._provider_model,
            )
            event_sink = observation.sink
            store = lifecycle.manage(
                self._session_store
                or ManagedResource[SessionStore](
                    name="session-store",
                    value=InMemorySessionStore(),
                    ownership="owned",
                )
            )
            if self._execution_resource is not None:
                lifecycle.manage(self._execution_resource)
            if self._lease_resource is not None:
                lifecycle.manage(self._lease_resource)
            dispatcher = (
                lifecycle.manage(self._recovery_resource)
                if self._recovery_resource is not None
                else None
            )
            if dispatcher is not None and self._distributed_execution is None:
                raise ConfigurationError("recovery requires distributed execution")
            if self._distributed_execution is not None and getattr(store, "supports_distributed_execution", False) is not True:
                raise ConfigurationError("distributed execution requires an explicitly supplied fencing-aware session reader")
            configured_plugins: list[tuple[str, PluginFactory]] = []
            if self._loaded_config is not None:
                catalog = (
                    self._capability_catalog
                    if self._capability_catalog is not None
                    else default_capability_catalog()
                )
                configured_plugins = [
                    (f"capability:{item.name}", catalog.plugin_for(item))
                    for item in self._loaded_config.spec.capabilities
                ]
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

            components = self._effective_profile().build_components(provider)

            session_plugin_host: PluginHost | None = None
            if configured_plugins or self._plugins:
                plugin_host = lifecycle.manage(
                    ManagedResource(
                        name="plugin-host",
                        value=PluginHost(event_sink=event_sink),
                        ownership="owned",
                    )
                )
                for name, factory in (*configured_plugins, *self._plugins):
                    await plugin_host.load(name, factory)
                capabilities = plugin_host.snapshot()
                if capabilities.tools or capabilities.next_turn_hook is not None or capabilities.middleware:
                    components = components.replace(
                        tools=components.tools + capabilities.tools,
                        middleware=components.middleware + capabilities.middleware,
                        next_turn_hook=_combine_hooks(
                            components.next_turn_hook, capabilities.next_turn_hook
                        ),
                    )
                if capabilities.session_plugin_names:
                    session_plugin_host = plugin_host

            resolver_resource = self._session_spec_resolver
            if self._session_spec_resolver_factory is not None:
                resolver_resource = ManagedResource(
                    "session-spec-resolver",
                    self._session_spec_resolver_factory(components),
                    "owned",
                )
            resolver = (
                lifecycle.manage(resolver_resource)
                if resolver_resource is not None
                else None
            )
            if resolver is not None and not callable(getattr(resolver, "resolve", None)):
                raise TypeError("session resolver must implement resolve")

            worker = None
            if dispatcher is not None:
                assert self._distributed_execution is not None
                worker = SessionRecoveryWorker(
                    self._distributed_execution, dispatcher,
                    settings=self._recovery_settings, event_sink=event_sink,
                )
                lifecycle.push("recovery-worker", worker.aclose)
            runtime = AgentRuntime(
                components,
                store,
                _observation=observation,
                distributed_execution=self._distributed_execution,
                recovery_worker=worker,
                plugin_host=session_plugin_host,
                session_pool_policy=self._session_pool_policy,
                session_spec_resolver=resolver,
                graceful_shutdown_seconds=self._graceful_shutdown_seconds,
                lifecycle=lifecycle,
                observer_lifecycle=observer_lifecycle,
            )
            if worker is not None:
                await worker.start()
            return runtime
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

    def _effective_profile(self) -> AgentProfile:
        profile = self._profile
        tool_factories = profile.tool_factories
        if self._tools is not None:
            tool_factories = tuple(_constant_factory(tool) for tool in self._tools)
        policy_factory = _override_factory(
            self._compaction_policy, profile.compaction_policy_factory
        )
        strategy_factory = _override_factory(
            self._compaction_strategy, profile.compaction_strategy_factory
        )
        if policy_factory is not None and strategy_factory is None:
            raise ConfigurationError("compaction policy requires a compaction strategy")
        return replace(
            profile,
            schema_validator_factory=_override_factory(
                self._schema_validator, profile.schema_validator_factory
            ),
            middleware_factories=(
                tuple(_constant_factory(item) for item in self._middleware)
                if self._middleware is not None else profile.middleware_factories
            ),
            tool_factories=tool_factories,
            approval_policy_factory=_override_factory(
                self._approval_policy, profile.approval_policy_factory
            ),
            execution_gate_factory=_override_factory(
                self._execution_gate, profile.execution_gate_factory
            ),
            context_strategy_factory=_override_factory(
                self._context_strategy, profile.context_strategy_factory
            ),
            compaction_policy_factory=policy_factory,
            compaction_strategy_factory=strategy_factory,
            next_turn_hook_factory=_override_factory(
                self._next_turn_hook, profile.next_turn_hook_factory
            ),
            system_prompt=(
                profile.system_prompt
                if self._system_prompt is _UNSET
                else cast(str | None, self._system_prompt)
            ),
            prompt_spec=profile.prompt_spec if self._system_prompt is _UNSET else None,
            max_model_requests=(
                profile.max_model_requests
                if self._max_model_requests is None
                else self._max_model_requests
            ),
        )

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


def _constant_factory(value: T) -> Callable[[], T]:
    return lambda: value


def _override_factory(
    override: T | None,
    factory: Callable[[], T] | None,
) -> Callable[[], T] | None:
    return _constant_factory(override) if override is not None else factory


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
