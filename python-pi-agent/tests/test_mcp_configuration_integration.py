"""Explicit full YAML -> SDK -> loopback HTTP integration."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

import yaml
from fixtures.mcp_http import local_peer
from pi_agent_py.api import (
    AgentRuntimeBuilder,
    AllowedMcpConnections,
    DeclaredSideEffectGate,
    Finish,
    JsonSchemaValidator,
    MappingSecretResolver,
    McpConnectionSpec,
    ProviderCatalog,
    ProviderRegistration,
    StaticApprovalPolicy,
    ToolCall,
    default_capability_catalog,
    default_provider_catalog,
    load_app_config,
    mcp_capability_registration,
    mcp_tool_name,
)
from pi_agent_py.testing import FakeProvider


@unittest.skipUnless(
    os.environ.get("PI_AGENT_MCP_INTEGRATION") == "1"
    and importlib.util.find_spec("mcp"),
    "enable local MCP integration and install [mcp]",
)
class McpYamlIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_yaml_http_uses_real_connector_options_and_explicit_secrets(self):
        with (
            local_peer("sse") as (url, calls, _, auth),
            tempfile.TemporaryDirectory() as directory,
        ):
            validator = JsonSchemaValidator()
            approved = McpConnectionSpec(
                "local",
                "streamable_http",
                url=url,
                headers={"Authorization": "fixture-only"},
            )
            catalog = default_capability_catalog().with_registration(
                mcp_capability_registration(
                    schema_validator=validator,
                    connection_policy=AllowedMcpConnections((approved,)),
                    secret_resolver=MappingSecretResolver(
                        {"LOCAL_AUTH": "fixture-only"}
                    ),
                )
            )
            provider = FakeProvider(
                [
                    [
                        ToolCall(
                            "c", mcp_tool_name("local", "echo"), {"value": "YAML HTTP"}
                        ),
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
            path = Path(directory) / "agent.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "provider": {"name": "deepseek"},
                        "capabilities": {
                            "mcp": {
                                "servers": {
                                    "local": {
                                        "transport": "streamable_http",
                                        "url": url,
                                        "allow_insecure_local": True,
                                        "header_refs": {"Authorization": "LOCAL_AUTH"},
                                        "tools": ["echo"],
                                        "limits": {"max_result_bytes": 2048},
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_app_config(
                path,
                api_key_override="unused",
                provider_catalog=providers,
                capability_catalog=catalog,
            )
            self.assertEqual(calls, [])
            self.assertNotIn("fixture-only", repr(loaded.spec.capabilities))
            async with await AgentRuntimeBuilder.from_loaded_config(
                loaded, provider_catalog=providers, capability_catalog=catalog
            ).with_schema_validator(validator).with_approval_policy(
                StaticApprovalPolicy(True)
            ).with_execution_gate(
                DeclaredSideEffectGate(allowed_side_effects=("external",))
            ).build() as runtime:
                self.assertEqual(
                    (await runtime.run("s", "q")).messages[2].content, "YAML HTTP"
                )
            self.assertEqual(calls, ["echo"])
            self.assertTrue(all(value == "fixture-only" for value in auth))


if __name__ == "__main__":
    unittest.main()
