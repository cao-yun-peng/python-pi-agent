"""Run the model-facing Bash tool through a real Docker process sandbox."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pi_agent_py import (
    Agent,
    AgentComponents,
    BashTool,
    DeclaredSideEffectGate,
    DockerProcessSandbox,
    FakeProvider,
    Finish,
    SandboxedBashExecutor,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--image",
        required=True,
        help="A pre-pulled Linux image that contains /bin/bash.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="The only host directory mounted into the container.",
    )
    parser.add_argument(
        "--workdir",
        default=".",
        help="POSIX path relative to the mounted workspace.",
    )
    args = parser.parse_args()

    process_sandbox = DockerProcessSandbox(
        args.workspace,
        image=args.image,
        network_enabled=False,
    )
    executor = SandboxedBashExecutor(
        process_sandbox,
        mode="workspace-write",
        default_timeout_seconds=30,
        max_timeout_seconds=60,
    )
    bash = BashTool(executor, requires_approval=True)
    provider = FakeProvider(
        [
            [
                ToolCall(
                    "call-bash",
                    "bash",
                    {
                        "command": (
                            "printf 'created inside Bash sandbox\\n' "
                            "> bash-executor-demo.txt && "
                            "printf 'stdout from Bash\\n' && "
                            "printf 'stderr from Bash\\n' >&2 && pwd"
                        ),
                        "workdir": args.workdir,
                        "timeout_seconds": 20,
                    },
                ),
                Finish("tool_calls"),
            ],
            [TextDelta("The Docker-backed Bash command settled."), Finish()],
        ]
    )
    components = AgentComponents(
        provider,
        tools=(bash,),
        approval_policy=StaticApprovalPolicy(
            True,
            reason="This deterministic example permits one workspace write.",
        ),
        execution_gate=DeclaredSideEffectGate(
            allowed_side_effects=("none", "write"),
            allowed_tools=("bash",),
        ),
    )
    agent = Agent(components)

    result = await agent.run("Create the Docker-backed Bash demo file.")
    for message in result.messages:
        if message.role == "tool":
            print(f"tool result:\n{message.content}")
    print(f"assistant: {result.message.content}")


if __name__ == "__main__":
    asyncio.run(main())
