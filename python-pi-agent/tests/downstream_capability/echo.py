from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pi_agent_py.api import (
    CapabilityConfigContext,
    CapabilityRegistration,
    CapabilitySettings,
    ConfigurationError,
    ContextPlan,
    FunctionTool,
    JSONObject,
    Message,
    NextTurnContext,
    PluginAPI,
    ProviderRequestContext,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)


@dataclass(frozen=True, slots=True)
class EchoSettings:
    prefix: str
    location: Path
    hooks: bool = False
    name: str = field(default="echo", init=False)


def parse_echo(
    raw: Mapping[str, object], context: CapabilityConfigContext
) -> EchoSettings:
    if set(raw) - {"prefix", "location", "hooks"}:
        raise ConfigurationError("unknown echo capability field")
    prefix = raw.get("prefix", "echo ")
    location = raw.get("location", "resource")
    hooks = raw.get("hooks", False)
    if not isinstance(prefix, str) or not isinstance(location, str):
        raise ConfigurationError("echo prefix and location must be strings")
    if type(hooks) is not bool:
        raise ConfigurationError("echo hooks must be a boolean")
    return EchoSettings(
        prefix, (context.config_path.parent / location).resolve(), hooks
    )


def echo_registration(
    trace: list[str], blocked: asyncio.Event | None = None
) -> CapabilityRegistration:
    class Hook:
        async def prepare(
            self, messages: Sequence[Message], *, context: NextTurnContext
        ) -> Sequence[Message]:
            trace.append("hook")
            return messages

    class SessionPlugin:
        async def before_provider_request(
            self, context: ProviderRequestContext, plan: ContextPlan
        ) -> ContextPlan:
            trace.append("session:before")
            return plan

        async def on_run_settled(self, run_id: str) -> None:
            trace.append("session:settled")

        async def close(self) -> None:
            trace.append("session:close")

    async def initialize(settings: CapabilitySettings, api: PluginAPI) -> None:
        if not isinstance(settings, EchoSettings):
            raise ConfigurationError("echo requires EchoSettings")
        trace.append("open:" + settings.prefix)
        api.register_cleanup(lambda: trace.append("close:" + settings.prefix))
        if settings.prefix == "fail":
            raise ValueError("private initialization diagnostic")
        if settings.prefix == "wait":
            if blocked is not None:
                blocked.set()
            await asyncio.Event().wait()

        async def execute(
            arguments: JSONObject, context: ToolExecutionContext
        ) -> ToolOutput:
            text = arguments["text"]
            if not isinstance(text, str):
                raise AssertionError("schema validation failed")
            trace.append("execute")
            return ToolOutput(settings.prefix + text)

        api.register_tool(
            FunctionTool(
                ToolDefinition(
                    "configured_echo",
                    "Echo configured text",
                    {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                ),
                execute,
            )
        )
        if settings.hooks:
            api.register_next_turn_hook(Hook())
            api.register_session_plugin(lambda context: SessionPlugin())

    return CapabilityRegistration("echo", initialize, parse_settings=parse_echo)
