"""Contracts for a downstream application importing the stable facade."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator, Sequence
from importlib.util import find_spec

import pi_agent_py.api as api
from pi_agent_py.api import (
    AgentEvent,
    AgentRuntimeBuilder,
    CallbackEventSink,
    EventSink,
    Finish,
    FunctionTool,
    InMemorySessionStore,
    JSONObject,
    Message,
    ModelProvider,
    ModelStreamEvent,
    PluginAPI,
    ProviderRequestContext,
    RuntimeBuildError,
    SessionStore,
    TextDelta,
    Tool,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    agent_event_record,
)


_STABLE_EXPORTS = frozenset(
    """
    Agent agent_event_record AGENT_EVENT_SCHEMA_VERSION AgentComponents AgentError
    AgentEvent AgentEventEnvelope AgentLoopLimitError AgentProfile AgentResult
    AgentRuntime AgentRuntimeBuilder AgentRuntimeError AgentSession ApprovalDecision
    AppConfigSpec ApprovalPolicy BufferedEventSink CallbackEventSink Cleanup coding_profile
    CompactionComponents CompactionDecision CompactionError CompactionNotNeededError
    CompactionPolicy CompactionStrategy ConfigurationError ContextPlan
    ContextPreparationError ContextStrategy ControlCancellation ControlClaim
    ControlDelivery ControlTransition DeclaredSideEffectGate default_provider_catalog
    DenyByDefaultApprovalPolicy DurableSessionEvent EventChannel EventKind
    EventProvenance EventRedactionPolicy EventSink EventSinkFailure Finish
    FullHistoryContextStrategy
    FunctionTool InMemoryEventSink InMemorySessionStore InternalError JsonlSessionStore
    JSONObject JSONValue load_app_config LoadedAppConfig Message minimal_profile
    ModelCompactionStrategy ModelProtocolError ModelProvider ModelStreamEvent
    NextTurnContext NextTurnHook PendingControl PluginAPI PluginError PluginFactory
    ProviderAuthenticationError ProviderCatalog ProviderError ProviderRateLimitError
    ProviderRequestContext ProviderRequestHook ProviderResilienceSettings ProviderState
    ProviderTimeoutError ProviderTransportError QueuedControl RedactingEventSink
    Registration ResourceOwnership RuntimeBuildError RuntimeCapacityError
    RuntimeCloseError RuntimeSession RuntimeState RuntimeStateError
    serialize_agent_event SessionCompaction SessionConflictError SessionControlError
    SessionEventContext SessionEventKind SessionPlugin SessionPluginContext
    SessionPluginFactory SessionPoolPolicy SessionReplayError SessionSnapshot
    SessionState SessionStorageError SessionStore SessionValidationError
    StaticApprovalPolicy TextDelta ThinkingDelta TokenBudgetContextStrategy
    TokenEstimator TokenThresholdCompactionPolicy TokenUsage Tool ToolCall
    ToolDefinition ToolExecutionContext ToolExecutionDeniedError ToolExecutionGate
    ToolExecutionGateError ToolOutput ToolSideEffects ToolValidationError
    EventFilter EventHub EventSubscription LoggingTelemetrySink ModelPrice
    OverflowPolicy PricingTable RunHandle TelemetryEvent TelemetrySink UsageLedger UsageSummary
    SessionOpenRequest SessionSpec SessionSpecResolver StaticSessionSpecResolver
    ProviderRegistration ProviderRuntimeConfig ProviderSettings ProviderSettingsParser
    parse_app_config format_config_diagnostics
    CapabilityCatalog CapabilityConfigContext CapabilityFactory CapabilityRegistration
    CapabilitySettings CapabilitySettingsParser ConfiguredCapability default_capability_catalog
    """.split()
)


class _AppProvider:
    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        if messages[-1].role == "tool":
            yield TextDelta(messages[-1].content)
            yield Finish()
        else:
            yield ToolCall(
                f"{context.run_id}:echo", tools[0].name, {"text": messages[-1].content}
            )
            yield Finish("tool_calls")


class PublicApiTests(unittest.TestCase):
    def test_functional_modules_supply_the_public_objects(self) -> None:
        from pi_agent_py.context.compaction import ModelCompactionStrategy
        from pi_agent_py.context.strategies import FullHistoryContextStrategy
        from pi_agent_py.observability.event_hub import EventHub
        from pi_agent_py.observability.events import CallbackEventSink
        from pi_agent_py.observability.telemetry import LoggingTelemetrySink
        from pi_agent_py.observability.usage import UsageLedger
        from pi_agent_py.storage.jsonl import JsonlSessionStore
        from pi_agent_py.storage.memory import InMemorySessionStore
        from pi_agent_py.tools.approval import (
            DenyByDefaultApprovalPolicy,
            StaticApprovalPolicy,
        )
        from pi_agent_py.tools.function import FunctionTool
        from pi_agent_py.tools.gates import DeclaredSideEffectGate

        objects = (
            ModelCompactionStrategy, FullHistoryContextStrategy, EventHub,
            CallbackEventSink, LoggingTelemetrySink, UsageLedger,
            JsonlSessionStore, InMemorySessionStore,
            DeclaredSideEffectGate, DenyByDefaultApprovalPolicy,
            FunctionTool, StaticApprovalPolicy,
        )
        for exported in objects:
            with self.subTest(exported=exported.__name__):
                self.assertIs(getattr(api, exported.__name__), exported)

    def test_tool_modules_supply_root_exports_without_package_aliases(self) -> None:
        import pi_agent_py
        import pi_agent_py.tools as tools
        from pi_agent_py.tools.approval import (
            DenyByDefaultApprovalPolicy,
            StaticApprovalPolicy,
        )
        from pi_agent_py.tools.function import FunctionTool
        from pi_agent_py.tools.gates import DeclaredSideEffectGate, TrustedToolExecutionGate
        from pi_agent_py.tools.sandbox import DockerProcessSandbox
        from pi_agent_py.tools.shell import BashTool, SandboxedBashExecutor, SandboxedShellTool
        from pi_agent_py.tools.validation import validate_tool_arguments

        for exported in (
            FunctionTool, SandboxedShellTool, TrustedToolExecutionGate,
            validate_tool_arguments, DockerProcessSandbox, BashTool,
            SandboxedBashExecutor, DenyByDefaultApprovalPolicy,
            StaticApprovalPolicy, DeclaredSideEffectGate,
        ):
            with self.subTest(exported=exported.__name__):
                self.assertIs(getattr(pi_agent_py, exported.__name__), exported)
                self.assertFalse(hasattr(tools, exported.__name__))

    def test_provider_runtime_modules_supply_root_exports(self) -> None:
        import pi_agent_py
        from pi_agent_py.providers.runtime import (
            AttemptFailure,
            AttemptSummary,
            CapabilityRoutePolicy,
            DefaultRetryPolicy,
            DirectModelRuntime,
            FullJitterBackoff,
            ProviderCandidate,
            ResilientModelRuntime,
            RetryDecision,
        )

        for exported in (
            AttemptFailure, AttemptSummary, CapabilityRoutePolicy,
            DefaultRetryPolicy, DirectModelRuntime, FullJitterBackoff,
            ProviderCandidate, ResilientModelRuntime, RetryDecision,
        ):
            with self.subTest(exported=exported.__name__):
                self.assertIs(getattr(pi_agent_py, exported.__name__), exported)

    def test_run_handle_is_defined_in_runtime(self) -> None:
        from pi_agent_py.runtime.run_handle import RunHandle

        self.assertIs(api.RunHandle, RunHandle)

    def test_application_modules_supply_the_public_objects(self) -> None:
        import pi_agent_py
        import pi_agent_py.application as application
        import pi_agent_py.application.capabilities as capabilities
        from pi_agent_py.application.builder import AgentRuntimeBuilder
        from pi_agent_py.application.capabilities.bash import bash_capability_registration
        from pi_agent_py.application.capability_catalog import CapabilityCatalog
        from pi_agent_py.application.configuration import AppConfigSpec, load_app_config
        from pi_agent_py.application.lifecycle import LifecycleScope, ManagedResource
        from pi_agent_py.application.profiles import AgentProfile, coding_profile
        from pi_agent_py.application.provider_catalog import (
            ProviderCatalog,
            ProviderRegistration,
            default_provider_catalog,
        )
        from pi_agent_py.application.runtime import AgentRuntime, RuntimeSession, SessionPoolPolicy
        from pi_agent_py.runtime.components import AgentComponents, CompactionComponents
        from pi_agent_py.runtime.session_spec import SessionSpec, StaticSessionSpecResolver

        for exported in (
            AgentRuntimeBuilder, CapabilityCatalog, AppConfigSpec, load_app_config,
            AgentProfile, coding_profile, ProviderCatalog, ProviderRegistration,
            default_provider_catalog, AgentRuntime, RuntimeSession, SessionPoolPolicy,
            AgentComponents, CompactionComponents, SessionSpec, StaticSessionSpecResolver,
        ):
            with self.subTest(exported=exported.__name__):
                self.assertIs(getattr(api, exported.__name__), exported)
                self.assertFalse(hasattr(application, exported.__name__))
        self.assertIs(pi_agent_py.LifecycleScope, LifecycleScope)
        self.assertIs(pi_agent_py.ManagedResource, ManagedResource)
        self.assertEqual(bash_capability_registration().name, "bash")
        self.assertFalse(hasattr(capabilities, "bash_capability_registration"))

    def test_relocated_module_paths_are_removed(self) -> None:
        for name in (
            "compaction", "session", "session_jsonl", "session_transitions",
            "events", "event_hub", "telemetry", "otel", "usage",
            "runtime._persistence", "runtime._observation",
            "shell", "sandbox",
            "model_runtime", "run_handle", "tools.core",
            "assembly", "app_runtime", "lifecycle", "configuration", "profiles",
            "provider_catalog", "capability_catalog", "_runtime_observation",
            "configured_bash", "components", "session_spec",
        ):
            with self.subTest(module=name):
                self.assertIsNone(find_spec(f"pi_agent_py.{name}"))

    def test_explicit_exports_match_the_stable_contract(self) -> None:
        self.assertCountEqual(api.__all__, _STABLE_EXPORTS)
        visible_names = {name for name in vars(api) if not name.startswith("_")}
        self.assertEqual(visible_names, _STABLE_EXPORTS)

    def test_legacy_exports_keep_the_same_objects(self) -> None:
        import pi_agent_py
        from pi_agent_py.application.runtime import AgentRuntime
        from pi_agent_py.ports import EventSink, ModelProvider, Tool
        from pi_agent_py.runtime import Agent, AgentSession

        overlap = _STABLE_EXPORTS.intersection(pi_agent_py.__all__)
        for name in overlap:
            with self.subTest(name=name):
                self.assertIs(getattr(api, name), getattr(pi_agent_py, name))

        self.assertIs(api.AgentRuntime, AgentRuntime)
        self.assertIs(api.Agent, Agent)
        self.assertIs(api.AgentSession, AgentSession)
        self.assertIs(api.ModelProvider, ModelProvider)
        self.assertIs(api.EventSink, EventSink)
        self.assertIs(api.Tool, Tool)
        for legacy_only in (
            "DockerProcessSandbox", "FakeProvider", "PluginHost", "replay_session"
        ):
            with self.subTest(legacy_only=legacy_only):
                self.assertIn(legacy_only, pi_agent_py.__all__)
                self.assertFalse(hasattr(api, legacy_only))


class DownstreamAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_runs_and_observes_a_session_using_only_the_api(self) -> None:
        events: list[AgentEvent] = []
        provider: ModelProvider = _AppProvider()
        store: SessionStore = InMemorySessionStore()
        sink: EventSink = CallbackEventSink(events.append)

        async def echo(
            arguments: JSONObject, context: ToolExecutionContext
        ) -> ToolOutput:
            return ToolOutput(content=f"{context.call.name}: {arguments['text']}")

        tool: Tool = FunctionTool(
            ToolDefinition(
                name="echo",
                description="Echo the supplied text.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            ),
            echo,
        )

        def plugin(api: PluginAPI) -> None:
            api.register_tool(tool)

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_session_store(store)
            .with_event_sink(sink)
            .with_plugin("echo", plugin)
            .build()
        )
        async with runtime, await runtime.open_session("app") as session:
            result = await session.run("hello")
            self.assertEqual(result.message.content, "echo: hello")
            self.assertEqual((await session.snapshot()).messages, result.messages)

        completed = next(event for event in events if event.kind == "run_completed")
        self.assertEqual(agent_event_record(completed)["kind"], "run_completed")
        self.assertEqual(completed.envelope.session_id, "app")
        self.assertEqual(runtime.state, "closed")

    async def test_build_errors_are_catchable_from_the_api(self) -> None:
        with self.assertRaises(RuntimeBuildError):
            await AgentRuntimeBuilder().build()
