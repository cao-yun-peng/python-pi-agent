"""Trace the model-facing Bash tool without starting a real process."""

from __future__ import annotations

import asyncio

from pi_agent_py import (
    Agent,
    AgentComponents,
    BashTool,
    DeclaredSideEffectGate,
    FakeProvider,
    Finish,
    ShellCollectedOutput,
    ShellExecRequest,
    ShellExecSpec,
    ShellRunResult,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
)


class DemoShellExecutor:
    """Deterministic ShellExecutor Provider used to expose the seam."""

    def resolve(self, request: ShellExecRequest) -> ShellExecSpec:
        spec = ShellExecSpec(
            command=request.command,
            workdir=request.workdir or "/workspace",
            timeout_seconds=min(request.timeout_seconds or 120.0, 600.0),
        )
        print(f"resolve: request={request!r}")
        print(f"resolve: spec={spec!r}")
        return spec

    async def execute(self, spec: ShellExecSpec) -> ShellRunResult:
        print(f"execute: command={spec.command!r} workdir={spec.workdir!r}")
        return ShellRunResult(
            exit_code=0,
            signal=None,
            timed_out=False,
            cancelled=False,
            timeout_seconds=spec.timeout_seconds,
            stdout=ShellCollectedOutput("5 tests passed\n"),
            stderr=ShellCollectedOutput(""),
        )


async def main() -> None:
    bash = BashTool(DemoShellExecutor())
    provider = FakeProvider(
        [
            [
                ToolCall(
                    "call-bash",
                    "bash",
                    {
                        "command": "python -m unittest -q",
                        "workdir": "python-pi-agent",
                        "timeout_seconds": 30,
                    },
                ),
                Finish("tool_calls"),
            ],
            [TextDelta("The deterministic test command passed."), Finish()],
        ]
    )
    components = AgentComponents(
        provider,
        tools=(bash,),
        approval_policy=StaticApprovalPolicy(
            True,
            reason="This offline example approves its scripted Bash call.",
        ),
        execution_gate=DeclaredSideEffectGate(
            allowed_side_effects=("none", "write"),
            allowed_tools=("bash",),
        ),
    )
    agent = Agent(components)

    result = await agent.run("Run the deterministic tests.")
    print(f"assistant: {result.message.content}")


if __name__ == "__main__":
    asyncio.run(main())
