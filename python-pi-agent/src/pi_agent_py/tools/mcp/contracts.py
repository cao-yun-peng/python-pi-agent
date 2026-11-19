"""SDK-free values, connection authorization and lifecycle ports."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from ..._metadata import _freeze_metadata
from ...domain import JSONObject, JSONValue
from ...errors import AgentError, ConfigurationError


class McpConnectionError(AgentError):
    """Connection startup or local authorization failed."""


class McpAuthenticationError(McpConnectionError):
    """An MCP endpoint rejected explicit credentials or access."""


class McpProtocolError(AgentError):
    """The peer violated the normalized MCP contract."""


class McpTransportError(AgentError):
    """A submitted request lost its transport; effects may have occurred."""


class McpResultError(AgentError):
    """A submitted request returned an unusable result."""


class McpRequestNotStartedError(McpTransportError):
    """A closed/broken/full owner rejected a request before submitting it."""


def _json(value: object, *, max_bytes: int, max_depth: int = 32) -> str:
    """Validate a parsed finite JSON tree before encoding it (not a wire cap)."""

    def visit(item: object, depth: int) -> None:
        if depth > max_depth:
            raise ValueError("MCP JSON exceeds nesting limit")
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("MCP JSON keys must be strings")
                visit(child, depth + 1)
            return
        raise ValueError("MCP value must be finite JSON")

    visit(value, 0)
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("MCP JSON exceeds byte limit")
    return encoded


@dataclass(frozen=True, slots=True)
class McpConnectionSpec:
    """Already resolved target; secrets are explicit and never included in repr."""

    server_id: str
    transport: Literal["stdio", "streamable_http"]
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    url: str | None = field(default=None, repr=False)
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,20}", self.server_id) is None:
            raise ConfigurationError("invalid MCP server_id")
        object.__setattr__(self, "args", tuple(self.args))
        for attr in ("env", "headers"):
            values = getattr(self, attr)
            if any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in values.items()
            ):
                raise ConfigurationError("MCP credentials must be string mappings")
            object.__setattr__(self, attr, _freeze_metadata(values))
        if any(not isinstance(arg, str) for arg in self.args):
            raise ConfigurationError("MCP args must be strings")
        if self.transport == "stdio":
            if (
                not self.command
                or not self.command.strip()
                or self.url is not None
                or self.headers
            ):
                raise ConfigurationError("stdio requires a command and no HTTP fields")
        elif self.transport == "streamable_http":
            target = urlsplit(self.url or "")
            if (
                target.scheme not in {"https", "http"}
                or not target.hostname
                or target.username is not None
                or target.password is not None
                or target.fragment
                or self.command is not None
                or self.args
                or self.cwd
                or self.env
            ):
                raise ConfigurationError(
                    "HTTP requires a URL without credentials and no stdio fields"
                )
        else:
            raise ConfigurationError("unsupported MCP transport")


class McpConnectionPolicy(Protocol):
    def check(self, spec: McpConnectionSpec) -> None:
        """Raise before any I/O unless this exact target is authorized."""
        ...


@dataclass(frozen=True, slots=True)
class AllowedMcpConnections:
    """Explicit exact-target allowlist; empty by default, no implicit localhost trust."""

    specs: tuple[McpConnectionSpec, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "specs", tuple(self.specs))

    def check(self, spec: McpConnectionSpec) -> None:
        if spec not in self.specs:
            raise McpConnectionError("MCP connection denied by local policy")


@dataclass(frozen=True, slots=True)
class McpLimits:
    max_discovered_tools: int = 256
    max_exposed_tools: int = 64
    max_schema_bytes: int = 64 * 1024
    max_catalog_bytes: int = 1024 * 1024
    max_result_bytes: int = 1024 * 1024
    max_structured_bytes: int = 128 * 1024
    max_content_chars: int = 128 * 1024
    max_depth: int = 32

    def __post_init__(self) -> None:
        for attr in self.__dataclass_fields__:
            value = getattr(self, attr)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigurationError("MCP budgets must be positive integers")


@dataclass(frozen=True, slots=True)
class McpServerSettings:
    spec: McpConnectionSpec
    allow_tools: tuple[str, ...]
    limits: McpLimits = field(default_factory=McpLimits)
    connection_timeout_seconds: float = 10
    call_timeout_seconds: float = 30
    cleanup_grace_seconds: float = 10

    def __post_init__(self) -> None:
        object.__setattr__(self, "allow_tools", tuple(self.allow_tools))
        if (
            not self.allow_tools
            or len(self.allow_tools) != len(set(self.allow_tools))
            or any(not isinstance(n, str) or not n.strip() for n in self.allow_tools)
            or len(self.allow_tools) > self.limits.max_exposed_tools
        ):
            raise ConfigurationError(
                "MCP requires a nonempty bounded unique tool allowlist"
            )
        for value in (
            self.connection_timeout_seconds,
            self.call_timeout_seconds,
            self.cleanup_grace_seconds,
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ConfigurationError("MCP deadlines must be finite and nonnegative")
        if self.cleanup_grace_seconds <= 0:
            raise ConfigurationError("MCP cleanup grace must be positive")


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    remote_name: str
    description: str
    input_schema: Mapping[str, JSONValue]
    output_schema: Mapping[str, JSONValue] | None = None
    annotations: Mapping[str, JSONValue] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.remote_name.strip():
            raise McpProtocolError("MCP remote tool name must be nonempty")
        for attr in ("input_schema", "output_schema", "annotations"):
            value = getattr(self, attr)
            if value is not None:
                object.__setattr__(self, attr, _freeze_metadata(value))


@dataclass(frozen=True, slots=True)
class McpContent:
    """Text or a typed omission marker. Never carry binary/resource payloads here."""

    kind: Literal["text", "image", "audio", "resource", "resource_link", "unsupported"]
    text: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {
            "text",
            "image",
            "audio",
            "resource",
            "resource_link",
            "unsupported",
        }:
            raise McpProtocolError("unsupported MCP content kind")
        if not isinstance(self.text, str) or (self.kind != "text" and self.text):
            raise McpProtocolError(
                "non-text MCP payloads must be omitted at the boundary"
            )


@dataclass(frozen=True, slots=True, init=False)
class McpCallResult:
    content: tuple[McpContent, ...]
    is_error: bool
    has_structured_content: bool
    _structured: str = field(repr=False)
    result_type: Literal["result", "input_required"]

    def __init__(
        self,
        content: tuple[McpContent, ...] = (),
        *,
        is_error: bool = False,
        structured_content: JSONValue = None,
        has_structured_content: bool = False,
        result_type: Literal["result", "input_required"] = "result",
    ) -> None:
        if not isinstance(is_error, bool) or not isinstance(
            has_structured_content, bool
        ):
            raise McpProtocolError("MCP result flags must be booleans")
        if structured_content is not None and not has_structured_content:
            raise McpProtocolError(
                "MCP structured content requires an explicit presence flag"
            )
        if result_type not in {"result", "input_required"} or any(
            not isinstance(block, McpContent) for block in content
        ):
            raise McpProtocolError("invalid normalized MCP result")
        try:
            encoded = _json(structured_content, max_bytes=1024 * 1024, max_depth=64)
        except (ValueError, TypeError, RecursionError) as error:
            raise McpResultError("invalid normalized MCP structured content") from error
        object.__setattr__(self, "content", tuple(content))
        object.__setattr__(self, "is_error", is_error)
        object.__setattr__(self, "has_structured_content", has_structured_content)
        object.__setattr__(self, "_structured", encoded)
        object.__setattr__(self, "result_type", result_type)

    @property
    def structured_content(self) -> JSONValue:
        return cast(JSONValue, json.loads(self._structured))


class McpEndpoint(Protocol):
    @property
    def protocol_version(self) -> str: ...

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]: ...

    async def call_tool(self, name: str, arguments: JSONObject) -> McpCallResult:
        """Submit once. Never automatically resend after failure or input_required."""
        ...

    async def aclose(self) -> None:
        """Release the endpoint idempotently, including after failed discovery."""
        ...


class McpConnector(Protocol):
    async def open(self, spec: McpConnectionSpec) -> McpEndpoint:
        """Own partial startup cleanup on error/cancellation; transfer ownership on return."""
        ...


def _validate_http_target(
    spec: McpConnectionSpec, *, allow_insecure_local: bool
) -> None:
    raw = spec.url or ""
    target = urlsplit(raw)
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ConfigurationError("MCP HTTP URL contains control characters")
    if target.scheme != "https" and not (
        allow_insecure_local
        and target.scheme == "http"
        and target.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ConfigurationError(
            "MCP HTTP requires HTTPS or explicit loopback development permission"
        )
    reserved = {
        "host",
        "content-type",
        "content-length",
        "accept",
        "accept-encoding",
        "connection",
        "transfer-encoding",
        "proxy-authorization",
        "mcp-session-id",
        "mcp-protocol-version",
        "last-event-id",
    }
    if any(
        name.lower() in reserved
        or "\r" in name
        or "\n" in name
        or "\r" in value
        or "\n" in value
        for name, value in spec.headers.items()
    ):
        raise ConfigurationError(
            "MCP credential headers contain reserved names or control characters"
        )
