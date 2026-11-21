from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from downstream_capability.echo import EchoSettings, echo_registration
from downstream_provider.echo import echo_registration as provider_registration
from pi_agent_py.api import (
    AgentEvent,
    AgentRuntimeBuilder,
    CallbackEventSink,
    CapabilityCatalog,
    CapabilityRegistration,
    CapabilitySettings,
    ConfigurationError,
    Finish,
    FunctionTool,
    JSONObject,
    RuntimeBuildError,
    ProviderCatalog,
    ProviderRegistration,
    PluginAPI,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    StaticApprovalPolicy,
    coding_profile,
    default_capability_catalog,
    default_provider_catalog,
    load_app_config,
    parse_app_config,
)
from pi_agent_py.application.capabilities.bash import DockerBashSettings
from pi_agent_py.testing import FakeProvider


def write_config(root: Path, body: str, *, provider_name: str = "deepseek") -> Path:
    path = root / "agent.yaml"
    path.write_text(
        f"version: 1\nprovider:\n  name: {provider_name}\n" + body,
        encoding="utf-8",
    )
    return path


class CapabilityConfigTests(unittest.TestCase):
    def test_external_parser_is_pure_typed_and_resolves_config_relative_paths(
        self,
    ) -> None:
        trace: list[str] = []
        original = default_capability_catalog()
        catalog = original.with_registration(echo_registration(trace))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_config(root, "capabilities:\n  echo:\n    prefix: 'custom '\n")
            spec = parse_app_config(path, capability_catalog=catalog)
            (configured,) = spec.capabilities
            self.assertIsInstance(configured.settings, EchoSettings)
            self.assertEqual(configured.name, "echo")
            self.assertEqual(configured.settings.location, (root / "resource").resolve())
            self.assertEqual(trace, [])
            self.assertEqual(original.names, ("bash",))
            with self.assertRaises(ConfigurationError):
                parse_app_config(path)
        with self.assertRaises(ValueError):
            catalog.with_registration(echo_registration(trace))

    def test_invalid_configurations_fail_before_initialization(self) -> None:
        trace: list[str] = []
        catalog = default_capability_catalog().with_registration(
            echo_registration(trace)
        )
        cases = (
            "capabilities: []\n",
            "capabilities:\n  echo: false\n",
            "capabilities:\n  echo:\n    unknown: 4\n",
            "capabilities:\n  echo:\n    hooks: 1\n",
            "capabilities:\n  echo: {}\n  echo: {}\n",
            "capabilities:\n  123: {}\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            for body in cases:
                with self.subTest(body=body), self.assertRaises(ConfigurationError):
                    parse_app_config(
                        write_config(Path(directory), body), capability_catalog=catalog
                    )
        self.assertEqual(trace, [])

    def test_bash_is_selected_only_through_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = parse_app_config(
                write_config(
                    root,
                    "capabilities:\n  bash:\n    provider: docker\n    image: local/test\n",
                )
            )
            (configured,) = spec.capabilities
            self.assertEqual(configured.name, "bash")
            self.assertIsInstance(configured.settings, DockerBashSettings)
            self.assertEqual(configured.settings.workspace_root, root.resolve())
            with self.assertRaisesRegex(ConfigurationError, "capabilities.bash"):
                parse_app_config(
                    write_config(
                        root,
                        "bash:\n  provider: docker\n  image: local/test\n"
                        "capabilities:\n  bash:\n    provider: docker\n    image: local/test\n",
                    )
                )

    def test_parser_failure_invalid_return_and_async_parser_are_rejected(self) -> None:
        def broken(raw, context):
            raise ValueError("private parser diagnostic")

        registration = echo_registration([])
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(Path(directory), "capabilities:\n  echo: {}\n")
            catalog = CapabilityCatalog((replace(registration, parse_settings=broken),))
            with self.assertRaises(ConfigurationError) as caught:
                parse_app_config(path, capability_catalog=catalog)
            self.assertIsInstance(caught.exception.__cause__, ValueError)
            self.assertNotIn("private", str(caught.exception))
            catalog = CapabilityCatalog(
                (
                    replace(
                        registration,
                        parse_settings=lambda raw, context: cast(
                            CapabilitySettings, None
                        ),
                    ),
                )
            )
            with self.assertRaises(ConfigurationError):
                parse_app_config(path, capability_catalog=catalog)

        async def async_parser(raw, context):
            return EchoSettings("a", Path.cwd())

        with self.assertRaises(TypeError):
            replace(registration, parse_settings=async_parser)


class CapabilityRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_tool_hook_and_session_plugin_run_and_close_in_order(
        self,
    ) -> None:
        trace: list[str] = []
        events: list[AgentEvent] = []
        catalog = default_capability_catalog().with_registration(
            echo_registration(trace)
        )

        class Provider(FakeProvider):
            async def aclose(self) -> None:
                trace.append("provider:close")

        provider = Provider(
            [
                [
                    ToolCall("call", "configured_echo", {"text": "hello"}),
                    Finish("tool_calls"),
                ],
                [Finish()],
            ]
        )
        # Use an external provider registration as well, exercising the B3/B4 seam.

        providers = default_provider_catalog().with_registration(
            replace(
                provider_registration(),
                factory=lambda config: provider,
                supports_tools=True,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                write_config(
                    Path(directory),
                    "capabilities:\n  echo:\n    prefix: 'custom '\n    hooks: true\n",
                    provider_name="echo",
                ),
                provider_catalog=providers,
                capability_catalog=catalog,
                api_key_override="offline-key",
            )
        runtime = await (
            AgentRuntimeBuilder.from_loaded_config(
                loaded, provider_catalog=providers, capability_catalog=catalog
            )
            .with_event_sink(CallbackEventSink(events.append))
            .build()
        )
        async with runtime:
            result = await runtime.run("s", "use the tool")
            self.assertIn(
                "custom hello", [message.content for message in result.messages]
            )
        self.assertEqual(
            [tool.name for tool in provider.requested_tools[0]], ["configured_echo"]
        )
        self.assertEqual(trace.count("hook"), 1)
        self.assertEqual(trace.count("session:before"), 2)
        self.assertEqual(trace.count("close:custom "), 1)
        self.assertLess(trace.index("session:close"), trace.index("close:custom "))
        self.assertLess(trace.index("close:custom "), trace.index("provider:close"))
        self.assertTrue(
            any(
                event.kind == "plugin_loaded"
                and event.payload["plugin_name"] == "capability:echo"
                for event in events
            )
        )
        self.assertTrue(
            any(event.kind == "tool_execution_finished" for event in events)
        )

    async def test_initialization_failure_rolls_back_registered_resources(self) -> None:
        trace: list[str] = []
        catalog = CapabilityCatalog((echo_registration(trace),))
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                write_config(
                    Path(directory), "capabilities:\n  echo:\n    prefix: fail\n"
                ),
                capability_catalog=catalog,
                api_key_override="offline-key",
            )

        builder = AgentRuntimeBuilder.from_loaded_config(
            loaded,
            provider_catalog=ProviderCatalog(
                (ProviderRegistration("deepseek", lambda config: FakeProvider([])),)
            ),
            capability_catalog=catalog,
        )
        with self.assertRaises(RuntimeBuildError) as caught:
            await builder.build()
        self.assertEqual(trace, ["open:fail", "close:fail"])
        self.assertNotIn("private", str(caught.exception))

    async def test_initialization_cancellation_runs_staged_cleanup_and_closes_provider(
        self,
    ) -> None:
        trace: list[str] = []
        blocked = asyncio.Event()
        catalog = CapabilityCatalog((echo_registration(trace, blocked),))

        class Provider(FakeProvider):
            async def aclose(self) -> None:
                trace.append("provider:close")

        providers = ProviderCatalog(
            (ProviderRegistration("deepseek", lambda config: Provider([])),)
        )
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                write_config(
                    Path(directory), "capabilities:\n  echo:\n    prefix: wait\n"
                ),
                capability_catalog=catalog,
                api_key_override="offline-key",
            )
        task = asyncio.create_task(
            AgentRuntimeBuilder.from_loaded_config(
                loaded,
                provider_catalog=providers,
                capability_catalog=catalog,
            ).build()
        )
        await blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(trace, ["open:wait", "close:wait", "provider:close"])

    async def test_profile_tool_conflict_rolls_back_configured_capability(self) -> None:
        trace: list[str] = []
        catalog = CapabilityCatalog((echo_registration(trace),))

        async def execute(
            arguments: JSONObject, context: ToolExecutionContext
        ) -> ToolOutput:
            return ToolOutput("profile tool")

        tool = FunctionTool(
            ToolDefinition("configured_echo", "Profile tool", {"type": "object"}),
            execute,
        )

        providers = ProviderCatalog(
            (ProviderRegistration("deepseek", lambda config: FakeProvider([])),)
        )
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                write_config(Path(directory), "capabilities:\n  echo: {}\n"),
                capability_catalog=catalog,
                api_key_override="offline-key",
            )
        with self.assertRaises(RuntimeBuildError):
            await (
                AgentRuntimeBuilder.from_loaded_config(
                    loaded, provider_catalog=providers, capability_catalog=catalog
                )
                .with_tools((tool,))
                .build()
            )
        self.assertEqual(trace, ["open:echo ", "close:echo "])

    async def test_missing_capability_catalog_is_rejected_before_provider_construction(
        self,
    ) -> None:
        trace: list[str] = []
        catalog = CapabilityCatalog((echo_registration(trace),))
        created: list[str] = []
        closed: list[str] = []

        class Sink:
            def emit(self, event: AgentEvent) -> None:
                pass

            async def aclose(self) -> None:
                closed.append("observer")

        def factory(config):
            created.append("provider")
            return FakeProvider([])

        providers = ProviderCatalog((ProviderRegistration("deepseek", factory),))
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                write_config(Path(directory), "capabilities:\n  echo: {}\n"),
                capability_catalog=catalog,
                api_key_override="offline-key",
            )
        with self.assertRaises(RuntimeBuildError):
            await AgentRuntimeBuilder.from_loaded_config(
                loaded,
                provider_catalog=providers,
            ).with_event_sink(Sink(), ownership="owned").build()
        self.assertEqual(created, [])
        self.assertEqual(trace, [])
        self.assertEqual(closed, ["observer"])

    async def test_canonical_bash_preserves_approval_side_effects_and_fail_closed_execution(
        self,
    ) -> None:

        provider = FakeProvider(
            [
                [
                    ToolCall("bash-call", "bash", {"command": "echo example"}),
                    Finish("tool_calls"),
                ],
                [Finish()],
            ]
        )
        providers = ProviderCatalog(
            (ProviderRegistration("deepseek", lambda config: provider),)
        )
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                write_config(
                    Path(directory),
                    "capabilities:\n  bash:\n    provider: docker\n"
                    "    image: local/test\n    docker_executable: missing-capability-test-docker\n"
                    "    network_enabled: true\n",
                ),
                api_key_override="offline-key",
            )
            runtime = await (
                AgentRuntimeBuilder.from_loaded_config(
                    loaded, provider_catalog=providers
                )
                .with_profile(coding_profile())
                .with_approval_policy(StaticApprovalPolicy(True))
                .build()
            )
            async with runtime:
                result = await runtime.run("bash", "use bash")
        (definition,) = provider.requested_tools[0]
        self.assertEqual(definition.side_effects, "external")
        self.assertTrue(definition.requires_approval)
        (tool_result,) = [
            message for message in result.messages if message.role == "tool"
        ]
        self.assertTrue(tool_result.is_error)
        self.assertIn("SandboxUnavailableError", tool_result.content)

    async def test_later_failure_unwinds_capabilities_in_reverse_configuration_order(
        self,
    ) -> None:
        trace: list[str] = []

        @dataclass(frozen=True)
        class Settings:
            name: str

        def parse_first(raw, context):
            return Settings("first")

        def parse_second(raw, context):
            return Settings("second")

        def initialize(settings: CapabilitySettings, api: PluginAPI) -> None:
            trace.append("open:" + settings.name)
            api.register_cleanup(lambda: trace.append("close:" + settings.name))
            if settings.name == "second":
                raise ValueError("initialization failed")

        catalog = CapabilityCatalog(
            (
                CapabilityRegistration("first", initialize, parse_first),
                CapabilityRegistration("second", initialize, parse_second),
            )
        )

        class Provider(FakeProvider):
            async def aclose(self) -> None:
                trace.append("provider:close")

        providers = ProviderCatalog(
            (ProviderRegistration("deepseek", lambda config: Provider([])),)
        )
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                write_config(
                    Path(directory), "capabilities:\n  first: {}\n  second: {}\n"
                ),
                capability_catalog=catalog,
                api_key_override="offline-key",
            )
        with self.assertRaises(RuntimeBuildError):
            await AgentRuntimeBuilder.from_loaded_config(
                loaded,
                provider_catalog=providers,
                capability_catalog=catalog,
            ).build()
        self.assertEqual(
            trace,
            [
                "open:first",
                "open:second",
                "close:second",
                "close:first",
                "provider:close",
            ],
        )
