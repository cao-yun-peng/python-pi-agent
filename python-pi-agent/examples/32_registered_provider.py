"""Register an external settings parser and run a FakeProvider offline."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    CallbackEventSink,
    ConfigurationError,
    Finish,
    ProviderRegistration,
    ProviderRuntimeConfig,
    TextDelta,
    default_provider_catalog,
    format_config_diagnostics,
    load_app_config,
)
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled


@dataclass(frozen=True, slots=True)
class DemoConfig:
    api_key: str = field(repr=False)
    model: str
    prefix: str


@dataclass(frozen=True, slots=True)
class DemoSettings:
    model: str = "offline-v1"
    prefix: str = "Demo "
    api_key_env: str = "DEMO_API_KEY"
    name: str = field(default="demo", init=False)

    def build(self, api_key: str) -> DemoConfig:
        return DemoConfig(api_key, self.model, self.prefix)


def parse_demo(raw: Mapping[str, object]) -> DemoSettings:
    if set(raw) - {"name", "model", "prefix", "api_key_env"}:
        raise ConfigurationError("unknown demo provider field")
    model = raw.get("model", "offline-v1")
    prefix = raw.get("prefix", "Demo ")
    variable = raw.get("api_key_env", "DEMO_API_KEY")
    if not isinstance(model, str) or not model.strip():
        raise ConfigurationError("demo model must be a non-empty string")
    if not isinstance(prefix, str) or not isinstance(variable, str):
        raise ConfigurationError("demo prefix and api_key_env must be strings")
    return DemoSettings(model, prefix, variable)


class DemoProvider(FakeProvider):
    async def aclose(self) -> None:
        print("provider closed")


def create_demo(config: ProviderRuntimeConfig) -> DemoProvider:
    if not isinstance(config, DemoConfig):
        raise ConfigurationError("demo factory requires DemoConfig")
    return DemoProvider(
        [
            [TextDelta(config.prefix + config.model), Finish()],
            [WaitUntilCancelled()],
        ]
    )


async def main() -> None:
    catalog = default_provider_catalog().with_registration(
        ProviderRegistration(
            "demo",
            create_demo,
            parse_settings=parse_demo,
        )
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "agent.yaml"
        path.write_text(
            "version: 1\nprovider:\n  name: demo\n  model: offline-v2\n"
            "  prefix: 'Registered '\n",
            encoding="utf-8",
        )
        loaded = load_app_config(
            path,
            provider_catalog=catalog,
            environ={},
            api_key_override="offline-demo-key",
        )
        print(format_config_diagnostics(loaded))
        runtime = await (
            AgentRuntimeBuilder.from_loaded_config(loaded, provider_catalog=catalog)
            .with_event_sink(
                CallbackEventSink(
                    lambda event: (
                        print(event.kind)
                        if event.kind
                        in {
                            "run_started",
                            "run_completed",
                            "run_cancelled",
                            "run_settled",
                        }
                        else None
                    )
                )
            )
            .build()
        )
    async with runtime, await runtime.open_session("demo") as session:
        print((await session.run("hello")).message.content)
        handle = await session.submit("wait")
        async for event in handle.events():
            if event.kind == "model_request_started":
                await handle.abort()
        try:
            await handle.result()
        except asyncio.CancelledError:
            print("result cancelled")


if __name__ == "__main__":
    asyncio.run(main())
