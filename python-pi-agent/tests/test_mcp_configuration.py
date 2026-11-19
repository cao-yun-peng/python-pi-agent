"""Pure settings, explicit secret resolution and existing Catalog integration."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    CapabilityConfigContext,
    DeclaredSideEffectGate,
    Finish,
    JsonSchemaValidator,
    ProviderCatalog,
    ProviderRegistration,
    RuntimeBuildError,
    StaticApprovalPolicy,
    ToolCall,
    default_capability_catalog,
    default_provider_catalog,
    load_app_config,
)
from pi_agent_py.errors import ConfigurationError
from pi_agent_py.testing import FakeMcpConnector, FakeMcpEndpoint, FakeProvider
from pi_agent_py.application.capabilities.mcp import (
    MappingSecretResolver,
    mcp_capability_registration,
    parse_mcp_settings,
)
from pi_agent_py.tools.mcp.contracts import (
    AllowedMcpConnections,
    McpCallResult,
    McpConnectionSpec,
    McpContent,
    McpToolDescriptor,
)
from pi_agent_py.tools.mcp.tool import mcp_tool_name


class McpSettingsTests(unittest.TestCase):
    def test_parser_is_pure_resolves_paths_and_freezes_only_secret_references(self):
        context = CapabilityConfigContext(
            Path(__file__).resolve().with_name("agent.yaml")
        )
        raw = {
            "servers": {
                "local": {
                    "transport": "stdio",
                    "command": "bin/server",
                    "cwd": "workspace",
                    "args": ["--exact", "$NO_EXPANSION"],
                    "tools": ["echo"],
                    "env_refs": {"TOKEN": "DEMO_TOKEN"},
                }
            }
        }
        parsed = parse_mcp_settings(raw, context)
        server = parsed.servers[0]
        self.assertEqual(
            server.settings.spec.command,
            str((context.config_path.parent / "bin/server").resolve()),
        )
        self.assertEqual(server.settings.spec.args[-1], "$NO_EXPANSION")
        self.assertEqual(server.settings.spec.env, {})
        raw["servers"]["local"]["env_refs"].clear()
        self.assertEqual(server.env_refs, {"TOKEN": "DEMO_TOKEN"})
        self.assertEqual(default_capability_catalog().names, ("bash",))

    def test_invalid_settings_are_rejected_without_reading_credentials(self):
        context = CapabilityConfigContext(Path(__file__).resolve())
        cases = [
            {"servers": {}},
            {
                "servers": {
                    "x": {"transport": "stdio", "command": "server", "tools": []}
                }
            },
            {
                "servers": {
                    "x": {
                        "transport": "stdio",
                        "command": "server",
                        "tools": ["echo"],
                        "env": {"TOKEN": "private"},
                    }
                }
            },
            {
                "servers": {
                    "x": {
                        "transport": "streamable_http",
                        "url": "http://example.test/mcp",
                        "allow_insecure_local": True,
                        "tools": ["echo"],
                    }
                }
            },
            {
                "servers": {
                    "x": {
                        "transport": "stdio",
                        "command": "server",
                        "tools": ["echo"],
                        "limits": {"max_depth": True},
                    }
                }
            },
        ]
        for raw in cases:
            with self.assertRaises(ConfigurationError):
                parse_mcp_settings(raw, context)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "install [jsonschema]")
class McpConfiguredRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_header_controls_are_rejected_before_policy_or_connection(
        self,
    ):
        from pi_agent_py.plugins import PluginHost
        from pi_agent_py.errors import PluginLoadError

        connector = FakeMcpConnector({})

        class Policy:
            def check(self, spec):
                raise AssertionError("invalid resolved headers reached authorization")

        catalog = default_capability_catalog().with_registration(
            mcp_capability_registration(
                schema_validator=JsonSchemaValidator(),
                connector=connector,
                connection_policy=Policy(),
                secret_resolver=MappingSecretResolver({"AUTH": "private\ninvalid"}),
            )
        )
        configured = catalog.parse_settings(
            "mcp",
            {
                "servers": {
                    "local": {
                        "transport": "streamable_http",
                        "url": "https://example.test/mcp",
                        "tools": ["echo"],
                        "header_refs": {"Authorization": "AUTH"},
                    }
                }
            },
            CapabilityConfigContext(Path(__file__).resolve()),
        )
        with self.assertRaises(PluginLoadError) as caught:
            await PluginHost().load("mcp", catalog.plugin_for(configured))
        self.assertIsInstance(caught.exception.__cause__, ConfigurationError)
        self.assertNotIn("private", str(caught.exception.__cause__))
        self.assertEqual(connector.opens, [])

    async def test_yaml_catalog_continues_model_and_resolves_credentials_before_open(
        self,
    ):
        endpoint = FakeMcpEndpoint(
            (McpToolDescriptor("echo", "Echo", {"type": "object"}),),
            (McpCallResult((McpContent("text", "configured"),)),),
        )
        connector = FakeMcpConnector({"local": endpoint})
        validator = JsonSchemaValidator()
        spec = McpConnectionSpec(
            "local",
            "streamable_http",
            url="https://example.test/mcp",
            headers={"Authorization": "fixture-only"},
        )
        resolver = MappingSecretResolver({"DEMO_AUTH": "fixture-only"})
        catalog = default_capability_catalog().with_registration(
            mcp_capability_registration(
                schema_validator=validator,
                connector=connector,
                secret_resolver=resolver,
                connection_policy=AllowedMcpConnections((spec,)),
            )
        )
        provider = FakeProvider(
            [
                [
                    ToolCall("c", mcp_tool_name("local", "echo"), {}),
                    Finish("tool_calls"),
                ],
                [Finish()],
            ]
        )
        providers = ProviderCatalog(
            (
                ProviderRegistration(
                    "deepseek",
                    lambda config: provider,
                    supports_tools=True,
                    parse_settings=lambda raw: (
                        default_provider_catalog().parse_settings("deepseek", raw)
                    ),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.yaml"
            path.write_text(
                "version: 1\nprovider:\n  name: deepseek\ncapabilities:\n  mcp:\n    servers:\n      local:\n        transport: streamable_http\n        url: https://example.test/mcp\n        header_refs: {Authorization: DEMO_AUTH}\n        tools: [echo]\n",
                encoding="utf-8",
            )
            loaded = load_app_config(
                path,
                api_key_override="unused",
                provider_catalog=providers,
                capability_catalog=catalog,
            )
            self.assertEqual(connector.opens, [])
            self.assertNotIn("fixture-only", repr(loaded.spec.capabilities))
            with self.assertRaises(RuntimeBuildError):
                await AgentRuntimeBuilder.from_loaded_config(
                    loaded, provider_catalog=providers
                ).build()
            self.assertEqual(connector.opens, [])
            runtime = await (
                AgentRuntimeBuilder.from_loaded_config(
                    loaded, provider_catalog=providers, capability_catalog=catalog
                )
                .with_schema_validator(validator)
                .with_approval_policy(StaticApprovalPolicy(True))
                .with_execution_gate(
                    DeclaredSideEffectGate(allowed_side_effects=("external",))
                )
                .build()
            )
            async with runtime:
                result = await runtime.run("s", "q")
            self.assertEqual(result.messages[2].content, "configured")
        self.assertEqual(connector.opens, [spec])
        self.assertEqual(endpoint.close_count, 1)

    async def test_missing_secret_or_connection_policy_denies_before_any_open(self):
        from pi_agent_py.plugins import PluginHost
        from pi_agent_py.errors import PluginLoadError

        for resolver in (
            MappingSecretResolver({}),
            MappingSecretResolver({"KEY": "fixture-only"}),
        ):
            connector = FakeMcpConnector({})
            catalog = default_capability_catalog().with_registration(
                mcp_capability_registration(
                    schema_validator=JsonSchemaValidator(),
                    connector=connector,
                    secret_resolver=resolver,
                )
            )
            configured = catalog.parse_settings(
                "mcp",
                {
                    "servers": {
                        "local": {
                            "transport": "stdio",
                            "command": "fixture",
                            "tools": ["echo"],
                            "env_refs": {"TOKEN": "KEY"},
                        }
                    }
                },
                CapabilityConfigContext(Path(__file__).resolve()),
            )
            with self.assertRaises(PluginLoadError):
                await PluginHost().load("mcp", catalog.plugin_for(configured))
            self.assertEqual(connector.opens, [])

    async def test_all_server_secrets_resolve_before_first_connection(self):
        from pi_agent_py.plugins import PluginHost
        from pi_agent_py.errors import PluginLoadError

        connector = FakeMcpConnector({})
        first = McpConnectionSpec(
            "one", "streamable_http", url="https://example.test/one"
        )
        catalog = default_capability_catalog().with_registration(
            mcp_capability_registration(
                schema_validator=JsonSchemaValidator(),
                connector=connector,
                secret_resolver=MappingSecretResolver({}),
                connection_policy=AllowedMcpConnections((first,)),
            )
        )
        configured = catalog.parse_settings(
            "mcp",
            {
                "servers": {
                    "one": {
                        "transport": "streamable_http",
                        "url": first.url,
                        "tools": ["echo"],
                    },
                    "two": {
                        "transport": "streamable_http",
                        "url": "https://example.test/two",
                        "tools": ["echo"],
                        "header_refs": {"Authorization": "MISSING"},
                    },
                }
            },
            CapabilityConfigContext(Path(__file__).resolve()),
        )
        with self.assertRaises(PluginLoadError):
            await PluginHost().load("mcp", catalog.plugin_for(configured))
        self.assertEqual(connector.opens, [])


if __name__ == "__main__":
    unittest.main()
