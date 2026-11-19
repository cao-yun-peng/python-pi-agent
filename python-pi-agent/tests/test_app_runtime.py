from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path
from typing import Never

from pi_agent_py import (
    AgentComponents,
    AgentEvent,
    AgentLoopLimitError,
    AgentProfile,
    AgentRuntimeBuilder,
    AppConfigSpec,
    CredentialDiagnostic,
    DeepSeekSettings,
    DockerBashSettings,
    DeclaredSideEffectGate,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    FullHistoryContextStrategy,
    FunctionTool,
    JSONObject,
    ContextPlan,
    LoadedAppConfig,
    InMemoryEventSink,
    Message,
    NextTurnContext,
    RuntimeBuildError,
    RuntimeCapacityError,
    RuntimeStateError,
    SessionPoolPolicy,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    TokenThresholdCompactionPolicy,
    StaticApprovalPolicy,
    WaitUntilCancelled,
    ProviderCatalog,
    ProviderRegistration,
    ProviderResilienceSettings,
    ProviderTimeoutError,
    coding_profile,
)
from pi_agent_py.api import ConfiguredCapability
from pi_agent_py.plugins import PluginAPI
from pi_agent_py.providers import DeepSeekConfig


class _ClosableProvider(FakeProvider):
    def __init__(self, responses, trace: list[str]) -> None:
        super().__init__(responses)
        self._trace = trace

    async def aclose(self) -> None:
        self._trace.append("provider")


class _ClosableSink:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    async def aclose(self) -> None:
        self.trace.append("sink")


class _RecordingHook:
    def __init__(self) -> None:
        self.calls: list[tuple[Message, ...]] = []

    async def prepare(
        self,
        messages: Sequence[Message],
        *,
        context: NextTurnContext,
    ) -> Sequence[Message]:
        del context
        self.calls.append(tuple(messages))
        return messages


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_component_fields_are_consumed_through_the_builder(self) -> None:
        from pi_agent_py.api import Middleware

        middleware_requests = []
        class RequestHook:
            async def prepare(self, context, plan):
                middleware_requests.append(context.request_number)
                return plan
        middleware = Middleware("record", provider_request=RequestHook())
        approval_policy = StaticApprovalPolicy(True)
        gate = DeclaredSideEffectGate(allowed_side_effects=("read",))
        compactor = FakeCompactionStrategy(["earlier work"])
        threshold = 1_000_000
        policy = TokenThresholdCompactionPolicy(threshold)
        hook = _RecordingHook()
        events = InMemoryEventSink()
        observed_contexts: list[tuple[Message, ...]] = []
        executed: list[str] = []

        class RecordingContext(FullHistoryContextStrategy):
            async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
                observed_contexts.append(tuple(messages))
                return await super().prepare(messages)

        async def echo(arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
            executed.append(context.call.id)
            return ToolOutput(content="echo result")

        tool = FunctionTool(
            ToolDefinition(
                "echo",
                "Echo a result",
                {"type": "object"},
                side_effects="read",
                requires_approval=True,
            ),
            echo,
        )
        provider = FakeProvider(
            [
                [ToolCall("one", "echo", {}), Finish("tool_calls")],
                [TextDelta("first"), Finish()],
                [TextDelta("second"), Finish()],
                [ToolCall("two", "echo", {}), Finish("tool_calls")],
                [ToolCall("three", "echo", {}), Finish("tool_calls")],
            ]
        )
        profile = AgentProfile(
            "combined",
            middleware_factories=(lambda: middleware,),
            system_prompt="Be concise.",
            tool_factories=(lambda: tool,),
            approval_policy_factory=lambda: approval_policy,
            execution_gate_factory=lambda: gate,
            context_strategy_factory=RecordingContext,
            compaction_policy_factory=lambda: policy,
            compaction_strategy_factory=lambda: compactor,
            next_turn_hook_factory=lambda: hook,
            max_model_requests=2,
        )
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_profile(profile)
            .with_event_sink(events)
            .build()
        )
        async with runtime, await runtime.open_session("components") as session:
            result = await session.run("first turn")
            await session.run("second turn")
            await session.compact()
            with self.assertRaisesRegex(AgentLoopLimitError, "exceeded 2"):
                await session.run("reach request limit")

        observations = {
            "middleware": (middleware_requests, [1, 2, 1, 1, 2]),
            "provider": (result.message.content, "first"),
            "tools": (provider.requested_tools[0], (tool.definition,)),
            "approval_policy": (len(approval_policy.requests), 3),
            "execution_gate": (executed, ["one", "two", "three"]),
            "context_strategy": (len(observed_contexts), 5),
            "compaction": (
                (
                    next(
                        event.payload["token_threshold"]
                        for event in events.events
                        if event.kind == "compaction_evaluated"
                    ),
                    len(compactor.requests),
                ),
                (threshold, 1),
            ),
            "next_turn_hook": (len(hook.calls), 2),
            "system_prompt": (provider.requests[0][0].content, "Be concise."),
            "max_model_requests": (len(provider.requests), 5),
        }
        self.assertEqual(set(observations), {field.name for field in fields(AgentComponents)})
        for component, (actual, expected) in observations.items():
            with self.subTest(component=component):
                self.assertEqual(actual, expected)

    async def test_builder_overrides_skip_replaced_factories(self) -> None:
        def unexpected_factory() -> Never:
            self.fail("an overridden profile factory was called")

        profile = AgentProfile(
            "defaults",
            system_prompt="profile default",
            tool_factories=(unexpected_factory,),
            context_strategy_factory=unexpected_factory,
            max_model_requests=3,
        )
        provider = FakeProvider(
            [[ToolCall("first", "missing", {}), Finish("tool_calls")]]
        )
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_profile(profile)
            .with_tools(())
            .with_context_strategy(FullHistoryContextStrategy())
            .with_max_model_requests(1)
            .with_system_prompt(None)
            .build()
        )
        async with runtime:
            with self.assertRaisesRegex(AgentLoopLimitError, "exceeded 1"):
                await runtime.run("override", "question")
        self.assertEqual(provider.requested_tools, [()])
        self.assertEqual(provider.requests, [(Message(role="user", content="question"),)])

    async def test_duplicate_profile_and_plugin_tools_roll_back_assembly(self) -> None:
        trace: list[str] = []

        async def echo(arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
            return ToolOutput(content="echo")

        async def cleanup() -> None:
            trace.append("plugin")

        tool = FunctionTool(ToolDefinition("echo", "Echo", {"type": "object"}), echo)

        def plugin(api: PluginAPI) -> None:
            api.register_tool(tool)
            api.register_cleanup(cleanup)

        builder = (
            AgentRuntimeBuilder()
            .with_provider(_ClosableProvider([], trace), ownership="owned")
            .with_profile(AgentProfile("duplicates", tool_factories=(lambda: tool,)))
            .with_plugin("duplicate", plugin)
        )
        with self.assertRaises(RuntimeBuildError) as error:
            await builder.build()

        self.assertIn("duplicate tool names: echo", str(error.exception.__cause__))
        self.assertEqual(trace, ["plugin", "provider"])

    async def test_configured_bash_missing_workspace_rolls_back_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider([])
            catalog = ProviderCatalog(
                (ProviderRegistration("deepseek", lambda _config: provider),)
            )
            loaded = LoadedAppConfig(
                spec=AppConfigSpec(
                    provider=DeepSeekSettings(),
                    config_path=root / "agent.yaml",
                    capabilities=(
                        ConfiguredCapability(
                            "bash",
                            DockerBashSettings(
                                workspace_root=root / "missing",
                                image="local/pi-agent:test",
                            ),
                        ),
                    ),
                ),
                provider_config=DeepSeekConfig(api_key="test-secret"),
                credential=CredentialDiagnostic(
                    variable="DEEPSEEK_API_KEY",
                    source="explicit",
                    masked_value="********cret",
                ),
            )

            with self.assertRaises(RuntimeBuildError) as raised:
                await AgentRuntimeBuilder.from_loaded_config(
                    loaded,
                    provider_catalog=catalog,
                ).build()

        self.assertEqual(
            str(raised.exception),
            "agent runtime assembly failed",
        )

    async def test_loaded_config_and_coding_profile_assemble_fail_closed_bash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            provider = FakeProvider(
                [
                    [
                        ToolCall("call-bash", "bash", {"command": "echo ok"}),
                        Finish("tool_calls"),
                    ],
                    [TextDelta("recovered"), Finish()],
                ]
            )
            catalog = ProviderCatalog(
                (ProviderRegistration("deepseek", lambda _config: provider),)
            )
            loaded = LoadedAppConfig(
                spec=AppConfigSpec(
                    provider=DeepSeekSettings(),
                    config_path=workspace / "agent.yaml",
                    capabilities=(
                        ConfiguredCapability(
                            "bash",
                            DockerBashSettings(
                                workspace_root=workspace,
                                image="local/pi-agent:test",
                                docker_executable="missing-pi-agent-docker-test",
                                network_enabled=True,
                            ),
                        ),
                    ),
                ),
                provider_config=DeepSeekConfig(api_key="test-secret"),
                credential=CredentialDiagnostic(
                    variable="DEEPSEEK_API_KEY",
                    source="explicit",
                    masked_value="********cret",
                ),
            )
            runtime = await (
                AgentRuntimeBuilder.from_loaded_config(
                    loaded,
                    provider_catalog=catalog,
                )
                .with_profile(coding_profile())
                .with_approval_policy(StaticApprovalPolicy(True))
                .build()
            )

            result = await runtime.run("session", "run a command")
            await runtime.aclose()

        self.assertEqual(
            [definition.name for definition in provider.requested_tools[0]],
            ["bash"],
        )
        self.assertEqual(
            provider.requested_tools[0][0].side_effects,
            "external",
        )
        self.assertTrue(provider.requested_tools[0][0].requires_approval)
        self.assertEqual(result.messages[2].role, "tool")
        self.assertTrue(result.messages[2].is_error)
        self.assertIn("SandboxUnavailableError", result.messages[2].content)
        self.assertEqual(result.message.content, "recovered")

    async def test_shutdown_cancels_active_run_after_zero_grace(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        events = InMemoryEventSink()
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_event_sink(events)
            .with_graceful_shutdown(0)
            .build()
        )
        run = asyncio.create_task(runtime.run("session", "wait"))
        await provider.blocked.wait()

        await runtime.aclose()

        with self.assertRaises(asyncio.CancelledError):
            await run
        self.assertEqual(runtime.state, "closed")
        self.assertEqual(
            sum(event.kind == "run_settled" for event in events.events),
            1,
        )

    async def test_loaded_config_uses_explicit_catalog_and_resilience(self) -> None:
        provider = FakeProvider(
            [
                [ProviderTimeoutError("temporary")],
                [TextDelta("retried"), Finish()],
            ]
        )
        catalog = ProviderCatalog(
            (ProviderRegistration("deepseek", lambda _config: provider),)
        )
        loaded = LoadedAppConfig(
            spec=AppConfigSpec(
                provider=DeepSeekSettings(),
                config_path=Path("agent.yaml"),
                resilience=ProviderResilienceSettings(
                    enabled=True,
                    base_delay_seconds=0,
                    max_delay_seconds=0,
                ),
            ),
            provider_config=DeepSeekConfig(api_key="test-secret"),
            credential=CredentialDiagnostic(
                variable="DEEPSEEK_API_KEY",
                source="explicit",
                masked_value="********cret",
            ),
        )
        runtime = await AgentRuntimeBuilder.from_loaded_config(
            loaded,
            provider_catalog=catalog,
        ).build()

        result = await runtime.run("session", "hello")
        await runtime.aclose()

        self.assertEqual(result.message.content, "retried")
        self.assertEqual(len(provider.requests), 2)

    async def test_profile_builds_reusable_session_and_persists_history(self) -> None:
        provider = FakeProvider(
            [
                [TextDelta("first"), Finish()],
                [TextDelta("second"), Finish()],
            ]
        )
        profile = AgentProfile(
            name="assistant",
            system_prompt="Be concise.",
        )
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_profile(profile)
            .build()
        )

        async with runtime:
            first = await runtime.open_session("session-1")
            second = await runtime.open_session("session-1")
            self.assertEqual(runtime.active_session_count, 1)
            async with first:
                result = await first.run("one")
                self.assertEqual(result.message.content, "first")
            async with second:
                result = await second.run("two")
                self.assertEqual(result.message.content, "second")
                snapshot = await second.snapshot()

        self.assertEqual(
            [message.role for message in snapshot.messages],
            ["system", "user", "assistant", "user", "assistant"],
        )
        self.assertEqual(provider.requests[0][0].content, "Be concise.")

    async def test_capacity_rejects_leased_session_then_evicts_idle_one(self) -> None:
        sink = _ClosableSink([])
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(FakeProvider([]))
            .with_event_sink(sink)
            .with_session_pool(
                SessionPoolPolicy(max_active_sessions=1, idle_ttl_seconds=None)
            )
            .build()
        )

        first = await runtime.open_session("one")
        with self.assertRaises(RuntimeCapacityError):
            await runtime.open_session("two")

        await first.close()
        second = await runtime.open_session("two")
        await second.close()

        self.assertEqual(runtime.active_session_count, 1)
        self.assertIn(
            "runtime_session_evicted",
            [event.kind for event in sink.events],
        )
        await runtime.aclose()

    async def test_zero_ttl_lazily_reopens_controller_from_durable_history(self) -> None:
        events = InMemoryEventSink()
        provider = FakeProvider(
            [
                [TextDelta("first"), Finish()],
                [TextDelta("second"), Finish()],
            ]
        )
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_event_sink(events)
            .with_session_pool(
                SessionPoolPolicy(max_active_sessions=1, idle_ttl_seconds=0)
            )
            .build()
        )

        await runtime.run("session", "one")
        result = await runtime.run("session", "two")
        snapshot = await runtime.snapshot("session")
        await runtime.aclose()

        self.assertEqual(result.message.content, "second")
        self.assertEqual(
            [message.role for message in snapshot.messages],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(
            sum(event.kind == "runtime_session_evicted" for event in events.events),
            1,
        )

    async def test_static_plugin_hook_is_composed_into_agent_session(self) -> None:
        hook = _RecordingHook()

        def plugin(api: PluginAPI) -> None:
            api.register_next_turn_hook(hook)

        provider = FakeProvider(
            [
                [
                    ToolCall("call-1", "missing", {}),
                    Finish("tool_calls"),
                ],
                [TextDelta("recovered"), Finish()],
            ]
        )
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider)
            .with_plugin("hook", plugin)
            .build()
        )

        result = await runtime.run("session", "use a tool")
        await runtime.aclose()

        self.assertEqual(result.message.content, "recovered")
        self.assertEqual(len(hook.calls), 1)
        self.assertEqual(hook.calls[0][-1].role, "tool")

    async def test_shutdown_order_is_plugin_provider_then_observer(self) -> None:
        trace: list[str] = []
        provider = _ClosableProvider([], trace)
        sink = _ClosableSink(trace)

        def plugin(api: PluginAPI) -> None:
            api.register_cleanup(lambda: trace.append("plugin"))

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider, ownership="owned")
            .with_event_sink(sink, ownership="owned")
            .with_plugin("owned-plugin", plugin)
            .build()
        )

        await runtime.aclose()
        await runtime.aclose()

        self.assertEqual(trace, ["plugin", "provider", "sink"])
        self.assertEqual(runtime.state, "closed")
        self.assertEqual(sink.events[-1].kind, "runtime_closed")
        with self.assertRaises(RuntimeStateError):
            await runtime.open_session("closed")

    async def test_failed_assembly_rolls_back_owned_resources(self) -> None:
        trace: list[str] = []
        provider = _ClosableProvider([], trace)
        sink = _ClosableSink(trace)

        def broken_plugin(api: PluginAPI) -> None:
            del api
            raise ValueError("private plugin failure")

        builder = (
            AgentRuntimeBuilder()
            .with_provider(provider, ownership="owned")
            .with_event_sink(sink, ownership="owned")
            .with_plugin("broken", broken_plugin)
        )

        with self.assertRaises(RuntimeBuildError) as raised:
            await builder.build()

        self.assertEqual(trace, ["provider", "sink"])
        self.assertNotIn("private", str(raised.exception))
        with self.assertRaises(RuntimeBuildError):
            await builder.build()


if __name__ == "__main__":
    unittest.main()
