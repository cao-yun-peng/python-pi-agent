"""Contracts for a downstream application importing the stable facade."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator, Sequence

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
    def test_explicit_exports_match_the_stable_contract(self) -> None:
        self.assertCountEqual(api.__all__, _STABLE_EXPORTS)
        visible_names = {name for name in vars(api) if not name.startswith("_")}
        self.assertEqual(visible_names, _STABLE_EXPORTS)

    def test_legacy_exports_keep_the_same_objects(self) -> None:
        import pi_agent_py
        from pi_agent_py.app_runtime import AgentRuntime
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
