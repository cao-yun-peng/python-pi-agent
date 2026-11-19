"""Configure an application-owned tool and observe its normal and rollback cleanup."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    CallbackEventSink,
    CapabilityConfigContext,
    CapabilityRegistration,
    CapabilitySettings,
    ConfigurationError,
    Finish,
    FunctionTool,
    JSONObject,
    PluginAPI,
    ProviderCatalog,
    ProviderRegistration,
    RuntimeBuildError,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    default_capability_catalog,
    load_app_config,
)
from pi_agent_py.testing import FakeProvider


@dataclass(frozen=True, slots=True)
class LabelSettings:
    prefix: str
    name: str = field(default="label", init=False)


def parse_label(
    raw: Mapping[str, object], context: CapabilityConfigContext
) -> LabelSettings:
    if set(raw) - {"prefix"}:
        raise ConfigurationError("unknown label capability field")
    prefix = raw.get("prefix", "Configured ")
    if not isinstance(prefix, str):
        raise ConfigurationError("label prefix must be a string")
    return LabelSettings(prefix)


class LabelResource:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.closed = False
        print("resource opened", prefix)

    def render(self, text: str) -> str:
        if self.closed:
            raise RuntimeError("label resource is closed")
        return self.prefix + text

    def close(self) -> None:
        self.closed = True
        print("resource closed", self.prefix)


def initialize_label(settings: CapabilitySettings, api: PluginAPI) -> None:
    if not isinstance(settings, LabelSettings):
        raise ConfigurationError("label requires LabelSettings")
    resource = LabelResource(settings.prefix)
    api.register_cleanup(resource.close)
    if settings.prefix == "fail":
        raise ConfigurationError("label initialization failed")

    async def execute(
        arguments: JSONObject, context: ToolExecutionContext
    ) -> ToolOutput:
        text = arguments["text"]
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        return ToolOutput(resource.render(text))

    api.register_tool(
        FunctionTool(
            ToolDefinition(
                "label",
                "Label supplied text",
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


async def main() -> None:
    catalog = default_capability_catalog().with_registration(
        CapabilityRegistration("label", initialize_label, parse_settings=parse_label)
    )
    providers = ProviderCatalog(
        (
            ProviderRegistration(
                "deepseek",
                lambda config: FakeProvider(
                    [
                        [
                            ToolCall("call", "label", {"text": "hello"}),
                            Finish("tool_calls"),
                        ],
                        [Finish()],
                    ]
                ),
            ),
        )
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "agent.yaml"
        for prefix in ("Configured ", "fail"):
            path.write_text(
                "version: 1\nprovider:\n  name: deepseek\n"
                f"capabilities:\n  label:\n    prefix: '{prefix}'\n",
                encoding="utf-8",
            )
            loaded = load_app_config(
                path,
                capability_catalog=catalog,
                environ={},
                api_key_override="offline-demo-key",
            )
            try:
                runtime = await (
                    AgentRuntimeBuilder.from_loaded_config(
                        loaded,
                        provider_catalog=providers,
                        capability_catalog=catalog,
                    )
                    .with_event_sink(
                        CallbackEventSink(
                            lambda event: (
                                print(event.kind)
                                if event.kind
                                in {
                                    "plugin_loaded",
                                    "tool_execution_finished",
                                    "run_settled",
                                }
                                else None
                            )
                        )
                    )
                    .build()
                )
            except RuntimeBuildError as error:
                print(type(error).__name__, "initialization rolled back")
                continue
            async with runtime:
                result = await runtime.run("demo", "label hello")
                print(
                    [
                        message.content
                        for message in result.messages
                        if message.role == "tool"
                    ]
                )


if __name__ == "__main__":
    asyncio.run(main())
