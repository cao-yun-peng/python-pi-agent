"""YAML -> Catalog -> explicit secrets/policy -> real local stdio -> FakeProvider."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import yaml

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
    TextDelta,
    ToolCall,
    default_capability_catalog,
    default_provider_catalog,
    load_app_config,
    mcp_capability_registration,
    mcp_tool_name,
)
from pi_agent_py.testing import FakeProvider


async def main() -> None:
    validator = JsonSchemaValidator()
    provider = FakeProvider(
        [
            [
                ToolCall(
                    "c", mcp_tool_name("local", "echo"), {"value": "Configured by YAML"}
                ),
                Finish("tool_calls"),
            ],
            [TextDelta("YAML capability completed."), Finish()],
        ]
    )
    providers = ProviderCatalog(
        (
            ProviderRegistration(
                "deepseek",
                lambda config: provider,
                supports_tools=True,
                parse_settings=lambda raw: default_provider_catalog().parse_settings(
                    "deepseek", raw
                ),
            ),
        )
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        fixture = Path(__file__).with_name("_mcp_demo_server.py").resolve()
        executable = str(Path(sys.executable).resolve())
        trace_path = root / "fixture.jsonl"
        # Only this code-created, reviewed target is approved. Never approve all
        # commands just because an untrusted YAML file asks to run them.
        approved = McpConnectionSpec(
            "local",
            "stdio",
            command=executable,
            args=("-B", str(fixture)),
            cwd=str(root),
            env={"MCP_DEMO_TRACE": str(trace_path)},
        )
        catalog = default_capability_catalog().with_registration(
            mcp_capability_registration(
                schema_validator=validator,
                connection_policy=AllowedMcpConnections((approved,)),
                secret_resolver=MappingSecretResolver(
                    {"DEMO_TRACE_FILE": str(trace_path)}
                ),
            )
        )
        path = root / "agent.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "provider": {"name": "deepseek"},
                    "capabilities": {
                        "mcp": {
                            "servers": {
                                "local": {
                                    "transport": "stdio",
                                    "command": executable,
                                    "args": ["-B", str(fixture)],
                                    "tools": ["echo"],
                                    "env_refs": {"MCP_DEMO_TRACE": "DEMO_TRACE_FILE"},
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
            api_key_override="offline-model-placeholder",
            provider_catalog=providers,
            capability_catalog=catalog,
        )
        async with await AgentRuntimeBuilder.from_loaded_config(
            loaded, provider_catalog=providers, capability_catalog=catalog
        ).with_schema_validator(validator).with_approval_policy(
            StaticApprovalPolicy(True)
        ).with_execution_gate(
            DeclaredSideEffectGate(allowed_side_effects=("external",))
        ).build() as runtime:
            result = await runtime.run("yaml-demo", "Call the configured tool")
            print(result.messages[2].content)
            print(result.message.content)
        print(
            "Fixture lifecycle:",
            [
                json.loads(line)["event"]
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ],
        )


if __name__ == "__main__":
    asyncio.run(main())
