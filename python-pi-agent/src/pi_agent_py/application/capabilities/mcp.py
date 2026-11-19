"""Pure MCP configuration and explicit secret/connection assembly."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal, Protocol, cast

from ...errors import ConfigurationError
from ...plugins import PluginAPI
from ...ports import SchemaValidator
from ...tools.mcp.contracts import (
    McpConnectionPolicy,
    McpConnectionSpec,
    McpConnector,
    McpLimits,
    McpServerSettings,
    _validate_http_target,
)
from ...tools.mcp.plugin import mcp_plugin
from ...tools.mcp.sdk import McpSdkOptions, SdkMcpConnector
from ..capability_catalog import (
    CapabilityConfigContext,
    CapabilityRegistration,
    CapabilitySettings,
)


class SecretResolver(Protocol):
    def resolve(self, name: str) -> str:
        """Resolve only an explicitly requested reference; never return environment maps."""
        ...


class MappingSecretResolver:
    """Application-owned snapshot of selected secrets; no environment/dotenv lookup."""

    def __init__(self, values: Mapping[str, str]) -> None:
        if any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()
        ):
            raise ConfigurationError("MCP secret source must be a string mapping")
        self._values = MappingProxyType(dict(values))

    def resolve(self, name: str) -> str:
        value = self._values.get(name)
        if not value:
            raise ConfigurationError("a required MCP secret reference is unavailable")
        return value


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    settings: McpServerSettings
    sdk_options: McpSdkOptions
    env_refs: Mapping[str, str] = field(default_factory=dict)
    header_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.settings.spec.env or self.settings.spec.headers:
            raise ConfigurationError(
                "MCP settings must store references rather than credentials"
            )
        for attribute in ("env_refs", "header_refs"):
            values = getattr(self, attribute)
            if any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in values.items()
            ):
                raise ConfigurationError("MCP references must be string mappings")
            object.__setattr__(self, attribute, MappingProxyType(dict(values)))
        if (self.settings.spec.transport == "stdio" and self.header_refs) or (
            self.settings.spec.transport == "streamable_http" and self.env_refs
        ):
            raise ConfigurationError("MCP reference type does not match its transport")


@dataclass(frozen=True, slots=True)
class McpCapabilitySettings:
    servers: tuple[McpServerConfig, ...]
    name: Literal["mcp"] = field(default="mcp", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "servers", tuple(self.servers))
        if not 1 <= len(self.servers) <= 16 or len(
            {s.settings.spec.server_id for s in self.servers}
        ) != len(self.servers):
            raise ConfigurationError("MCP capability requires 1..16 unique servers")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ConfigurationError("MCP configuration must be a string-keyed mapping")
    return dict(value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigurationError("MCP field requires a nonempty string without NUL")
    return value


def _number(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationError(
            "MCP configuration deadlines must be positive finite numbers"
        )
    return float(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError("MCP configuration bounds must be positive integers")
    return value


def _refs(value: object, *, headers: bool = False) -> dict[str, str]:
    refs = {key: _text(item) for key, item in _object(value).items()}
    for key, name in refs.items():
        key_pattern = (
            r"[!#$%&'*+.^_`|~0-9A-Za-z-]+" if headers else r"[A-Za-z_][A-Za-z0-9_]*"
        )
        if (
            re.fullmatch(key_pattern, key) is None
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        ):
            raise ConfigurationError(
                "invalid MCP credential reference or destination name"
            )
    return refs


def parse_mcp_settings(
    raw: Mapping[str, object], context: CapabilityConfigContext
) -> McpCapabilitySettings:
    if set(raw) != {"servers"}:
        raise ConfigurationError("MCP capability requires only a servers mapping")
    definitions = _object(raw["servers"])
    if not 1 <= len(definitions) <= 16:
        raise ConfigurationError("MCP capability requires 1..16 servers")
    result = []
    allowed = {
        "transport",
        "command",
        "args",
        "cwd",
        "url",
        "tools",
        "env_refs",
        "header_refs",
        "connection_timeout_seconds",
        "call_timeout_seconds",
        "cleanup_grace_seconds",
        "limits",
        "protocol_mode",
        "allow_insecure_local",
        "max_discovery_pages",
        "max_pending_requests",
    }
    for server_id, definition in definitions.items():
        values = _object(definition)
        if set(values) - allowed:
            raise ConfigurationError("unsupported MCP server configuration field")
        transport = values.get("transport")
        if not isinstance(transport, str) or transport not in {
            "stdio",
            "streamable_http",
        }:
            raise ConfigurationError("MCP transport must be stdio or streamable_http")
        tools = values.get("tools")
        if not isinstance(tools, list) or any(
            not isinstance(n, str) or not n.strip() for n in tools
        ):
            raise ConfigurationError(
                "MCP tools requires an explicit list of remote names"
            )
        limit_values = _object(values.get("limits", {}))
        if set(limit_values) - McpLimits.__dataclass_fields__.keys():
            raise ConfigurationError("unsupported MCP budget field")
        limits = McpLimits(
            **{key: _integer(value) for key, value in limit_values.items()}
        )
        connect = _number(values.get("connection_timeout_seconds", 10))
        call = _number(values.get("call_timeout_seconds", 30))
        grace = _number(values.get("cleanup_grace_seconds", 10))
        insecure = values.get("allow_insecure_local", False)
        if not isinstance(insecure, bool):
            raise ConfigurationError("MCP loopback permission must be boolean")
        options = McpSdkOptions(
            mode=cast(
                Literal["auto", "legacy", "2026-07-28"],
                _text(values.get("protocol_mode", "auto")),
            ),
            limits=limits,
            open_timeout_seconds=connect,
            request_timeout_seconds=call,
            cleanup_timeout_seconds=grace * 0.8,
            allow_insecure_local=insecure,
            max_discovery_pages=_integer(values.get("max_discovery_pages", 100)),
            max_pending_requests=_integer(values.get("max_pending_requests", 64)),
        )
        env_refs, header_refs = {}, {}
        if transport == "stdio":
            if any(
                key in values for key in ("url", "header_refs", "allow_insecure_local")
            ):
                raise ConfigurationError("HTTP fields are not valid for stdio")
            arguments = values.get("args", [])
            if not isinstance(arguments, list) or any(
                not isinstance(arg, str) or "\x00" in arg for arg in arguments
            ):
                raise ConfigurationError("MCP args must be exact string arguments")
            command = _text(values.get("command"))
            if command.startswith("!"):
                raise ConfigurationError("MCP command evaluation is not supported")
            base = context.config_path.parent
            spec = McpConnectionSpec(
                server_id,
                "stdio",
                command=str((base / command).resolve()),
                args=tuple(arguments),
                cwd=str((base / _text(values.get("cwd", "."))).resolve()),
            )
            env_refs = _refs(values.get("env_refs", {}))
        else:
            if any(key in values for key in ("command", "args", "cwd", "env_refs")):
                raise ConfigurationError("stdio fields are not valid for HTTP")
            spec = McpConnectionSpec(
                server_id, "streamable_http", url=_text(values.get("url"))
            )
            header_refs = _refs(values.get("header_refs", {}), headers=True)
            _validate_http_target(
                replace(spec, headers={key: "reference" for key in header_refs}),
                allow_insecure_local=insecure,
            )
        result.append(
            McpServerConfig(
                McpServerSettings(spec, tuple(tools), limits, connect, call, grace),
                options,
                env_refs,
                header_refs,
            )
        )
    return McpCapabilitySettings(tuple(result))


def mcp_capability_registration(
    *,
    schema_validator: SchemaValidator,
    connection_policy: McpConnectionPolicy | None = None,
    secret_resolver: SecretResolver | None = None,
    connector: McpConnector | None = None,
) -> CapabilityRegistration:
    resolver = (
        secret_resolver if secret_resolver is not None else MappingSecretResolver({})
    )

    async def initialize(settings: CapabilitySettings, api: PluginAPI) -> None:
        if not isinstance(settings, McpCapabilitySettings):
            raise ConfigurationError("MCP capability requires McpCapabilitySettings")
        resolved = []
        for server in settings.servers:
            try:
                env = {
                    key: resolver.resolve(name) for key, name in server.env_refs.items()
                }
                headers = {
                    key: resolver.resolve(name)
                    for key, name in server.header_refs.items()
                }
                if any(
                    not isinstance(value, str) or not value
                    for value in (*env.values(), *headers.values())
                ):
                    raise ConfigurationError(
                        "MCP resolver must return nonempty strings"
                    )
            except Exception as error:
                raise ConfigurationError("MCP secret resolution failed") from error
            spec = replace(server.settings.spec, env=env, headers=headers)
            if spec.transport == "streamable_http":
                _validate_http_target(
                    spec, allow_insecure_local=server.sdk_options.allow_insecure_local
                )
            resolved.append(replace(server.settings, spec=spec))
        # All required references resolve before any connection. The plugin checks
        # every resolved target's policy before opening the first endpoint.
        selected = (
            connector
            if connector is not None
            else SdkMcpConnector(
                per_server={
                    server.settings.spec.server_id: server.sdk_options
                    for server in settings.servers
                }
            )
        )
        result = mcp_plugin(
            tuple(resolved),
            connector=selected,
            schema_validator=schema_validator,
            connection_policy=connection_policy,
        )(api)
        if result is not None:
            await result

    return CapabilityRegistration("mcp", initialize, parse_settings=parse_mcp_settings)
