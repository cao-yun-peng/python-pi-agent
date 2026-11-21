"""Run a deterministic shell tool call inside a Docker workspace sandbox."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pi_agent_py import (
    Agent,
    AgentComponents,
    DeclaredSideEffectGate,
    DockerProcessSandbox,
    FakeProvider,
    Finish,
    SandboxedShellTool,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
)
from pi_agent_py.domain import SandboxProcessRequest


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--image",
        required=True,
        help="A pre-pulled Linux image that contains /bin/sh.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="The only host directory mounted into the container.",
    )
    args = parser.parse_args()

    process_sandbox = DockerProcessSandbox(
        args.workspace,
        image=args.image,
        network_enabled=False,
    )
    from pi_agent_py.domain import SandboxProcessRequest
    dummy_request = SandboxProcessRequest(
    argv=("echo", "hello"),
    workdir="/workspace",
    mode="read-only",
    timeout_seconds=None,
    )
    docker_cmd = process_sandbox.build_command(dummy_request, container_name="test")
    print("Docker 命令:", " ".join(docker_cmd))

    shell = SandboxedShellTool(
        process_sandbox,
        mode="workspace-write",
        requires_approval=True,
    )
    provider = FakeProvider(
        [
            [
                ToolCall(
                    "call-shell",
                    "shell",
                    {
                        "command": (
                            "printf 'created inside the sandbox\\n' "
                            "> sandbox-demo.txt && pwd && ls -l sandbox-demo.txt"
                        )
                    },
                ),
                Finish("tool_calls"),
            ],
            [TextDelta("The sandbox created sandbox-demo.txt."), Finish()],
        ]
    )
    components = AgentComponents(
        provider,
        tools=(shell,),
        approval_policy=StaticApprovalPolicy(
            True,
            reason="This deterministic example permits one workspace write.",
        ),
        execution_gate=DeclaredSideEffectGate(
            allowed_side_effects=("none", "write"),
            allowed_tools=("shell",),
        ),
    )
    agent = Agent(components)

    result = await agent.run("Create the sandbox demonstration file.")
    print(result.message.content)


if __name__ == "__main__":
    asyncio.run(main())
