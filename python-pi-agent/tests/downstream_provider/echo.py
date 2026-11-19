"""A registered provider implemented outside src/pi_agent_py."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from pi_agent_py.api import (
    ConfigurationError,
    Finish,
    Message,
    ModelStreamEvent,
    ProviderRegistration,
    ProviderRequestContext,
    ProviderRuntimeConfig,
    TextDelta,
    TokenUsage,
    ToolDefinition,
)


@dataclass(frozen=True, slots=True)
class EchoConfig:
    api_key: str = field(repr=False)
    model: str = "echo-v1"
    prefix: str = "echo "


@dataclass(frozen=True, slots=True)
class EchoSettings:
    model: str = "echo-v1"
    api_key_env: str = "ECHO_API_KEY"
    prefix: str = "echo "
    name: str = field(default="echo", init=False)

    def build(self, api_key: str) -> EchoConfig:
        return EchoConfig(api_key, self.model, self.prefix)


def parse_echo_settings(raw: Mapping[str, object]) -> EchoSettings:
    if set(raw) - {"name", "model", "api_key_env", "prefix"}:
        raise ConfigurationError("unknown echo provider field")
    model = raw.get("model", "echo-v1")
    variable = raw.get("api_key_env", "ECHO_API_KEY")
    prefix = raw.get("prefix", "echo ")
    if not isinstance(model, str) or not model.strip():
        raise ConfigurationError("echo model must be a non-empty string")
    if not isinstance(variable, str):
        raise ConfigurationError("echo api_key_env must be a string")
    if not isinstance(prefix, str):
        raise ConfigurationError("echo prefix must be a string")
    return EchoSettings(model, variable, prefix)


class EchoProvider:
    def __init__(self, config: EchoConfig) -> None:
        self.config = config
        self.requests: list[tuple[Message, ...]] = []
        self.blocked = asyncio.Event()
        self.closed = False

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(tuple(messages))
        if messages[-1].content == "wait":
            self.blocked.set()
            await asyncio.Event().wait()
        yield TextDelta(self.config.prefix + messages[-1].content)
        yield TokenUsage(2, 1, 3)
        yield Finish()

    async def aclose(self) -> None:
        self.closed = True


def make_echo_provider(config: ProviderRuntimeConfig) -> EchoProvider:
    if not isinstance(config, EchoConfig):
        raise ConfigurationError("echo factory requires EchoConfig")
    return EchoProvider(config)


def echo_registration() -> ProviderRegistration:
    return ProviderRegistration(
        "echo",
        make_echo_provider,
        supports_tools=False,
        parse_settings=parse_echo_settings,
    )
