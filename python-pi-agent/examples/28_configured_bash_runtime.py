"""Assemble Docker-backed Bash from YAML while using a fake model provider."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pi_agent_py import (
    AgentRuntimeBuilder,
    FakeProvider,
    Finish,
    ProviderCatalog,
    ProviderRegistration,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
    coding_profile,
    load_app_config,
)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Explicit agent.yaml containing a capabilities.bash section.",
    )
    args = parser.parse_args()

    loaded = load_app_config(
        args.config,
        api_key_override="example-key-not-used-by-fake-provider",
    )
    if all(item.name != "bash" for item in loaded.spec.capabilities):
        parser.error(
            "the selected configuration does not define capabilities.bash"
        )

    provider = FakeProvider(
        [
            [
                ToolCall(
                    "call-bash",
                    "bash",
                    {
                        "command": "pwd && printf 'configured Bash is running\\n'",
                        "workdir": ".",
                        "timeout_seconds": 20,
                    },
                ),
                Finish("tool_calls"),
            ],
            [TextDelta("The configured Docker Bash command settled."), Finish()],
        ]
    )
    catalog = ProviderCatalog(
        (
            ProviderRegistration(
                loaded.provider_name,
                lambda _config: provider,
            ),
        )
    )
    runtime = await (
        AgentRuntimeBuilder.from_loaded_config(
            loaded,
            provider_catalog=catalog,
        )
        .with_profile(coding_profile())
        .with_approval_policy(
            StaticApprovalPolicy(
                True,
                reason="This example approves one fixed read-only command.",
            )
        )
        .build()
    )

    try:
        result = await runtime.run("configured-bash", "Show the sandbox workdir.")
        for message in result.messages:
            if message.role == "tool":
                print(f"tool result:\n{message.content}")
        print(f"assistant: {result.message.content}")
    finally:
        await runtime.aclose()


if __name__ == "__main__":
    asyncio.run(main())
