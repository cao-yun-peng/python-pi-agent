"""Select per-Session tools, prompts, and observation metadata without an API key."""

from __future__ import annotations

import asyncio

from pi_agent_py.api import (
    AgentComponents,
    AgentRuntimeBuilder,
    CallbackEventSink,
    ConfigurationError,
    Finish,
    FunctionTool,
    JSONObject,
    RuntimeStateError,
    SessionOpenRequest,
    SessionSpec,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from pi_agent_py.testing import FakeProvider


def make_tool(name: str) -> FunctionTool:
    async def execute(
        arguments: JSONObject, context: ToolExecutionContext
    ) -> ToolOutput:
        return ToolOutput(f"{name} completed")

    return FunctionTool(
        ToolDefinition(name, f"Run {name}", {"type": "object"}), execute
    )


class RoleResolver:
    """An application's explicit profile selection with cached capability instances."""

    def __init__(self, base: AgentComponents) -> None:
        self._components = {
            name: base.replace(
                system_prompt=f"You are the {name} assistant.",
                tools=(make_tool(name),),
            )
            for name in ("research", "support")
        }

    async def resolve(
        self, session_id: str, request: SessionOpenRequest
    ) -> SessionSpec:
        if request.profile not in self._components or request.tenant_id is None:
            raise ConfigurationError("a known profile and tenant are required")
        return SessionSpec(
            self._components[request.profile],
            {"tenant_id": request.tenant_id, "profile": request.profile},
        )


async def main() -> None:
    provider = FakeProvider(
        [
            [ToolCall("lookup", "research", {}), Finish("tool_calls")],
            [TextDelta("Research done."), Finish()],
            [ToolCall("help", "support", {}), Finish("tool_calls")],
            [TextDelta("Support done."), Finish()],
        ]
    )
    runtime = await (
        AgentRuntimeBuilder()
        .with_provider(provider)
        .with_session_spec_resolver_factory(RoleResolver)
        .with_event_sink(
            CallbackEventSink(
                lambda event: (
                    print(event.kind, dict(event.envelope.metadata))
                    if event.kind in {"runtime_session_opened", "run_completed"}
                    else None
                )
            )
        )
        .build()
    )
    async with runtime:
        for profile, tenant in (("research", "team-a"), ("support", "team-b")):
            async with await runtime.open_session(
                profile, request=SessionOpenRequest(profile=profile, tenant_id=tenant)
            ) as session:
                print((await session.run("Start.")).message.content)
                print("metadata", dict(session.metadata))
        try:
            await runtime.open_session(
                "research",
                request=SessionOpenRequest(profile="support", tenant_id="team-a"),
            )
        except RuntimeStateError as error:
            print(type(error).__name__, str(error))


if __name__ == "__main__":
    asyncio.run(main())
