"""Project one discovered remote tool into the ordinary Tool port."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from copy import deepcopy
from uuid import uuid4

from ...domain import (
    EventKind,
    JSONObject,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from ...errors import ConfigurationError, ToolValidationError
from ...observability.events import (
    NullEventSink,
    ensure_isolated_event_sink,
    make_agent_event,
)
from ...ports import EventSink, SchemaValidator
from .contracts import (
    McpCallResult,
    McpConnectionError,
    McpEndpoint,
    McpProtocolError,
    McpResultError,
    McpRequestNotStartedError,
    McpServerSettings,
    McpToolDescriptor,
    McpTransportError,
    _json,
)


def mcp_tool_name(server_id: str, remote_name: str) -> str:
    """Order-independent, model-compatible name; never used as the remote name."""
    if (
        re.fullmatch(r"[A-Za-z0-9_-]{1,20}", server_id) is None
        or not remote_name.strip()
    ):
        raise ConfigurationError("invalid MCP naming identity")
    digest = hashlib.sha256(
        json.dumps([server_id, remote_name], ensure_ascii=False).encode()
    ).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", remote_name).strip("_") or "tool"
    prefix = f"mcp__{server_id}__"
    return f"{prefix}{slug[: 64 - len(prefix) - 14]}__{digest}"


def _output_wrapper(schema: JSONObject, digest: str) -> JSONObject:
    # A nested resource keeps local $refs rooted at the original output schema.
    resource = deepcopy(schema)
    resource.setdefault("$id", f"urn:pi-agent:mcp:output:{digest}")
    return {
        "type": "object",
        "properties": {"result": resource},
        "required": ["result"],
    }


class McpTool:
    """One binding, one call, conservative local permissions and bounded projection."""

    def __init__(
        self,
        settings: McpServerSettings,
        descriptor: McpToolDescriptor,
        endpoint: McpEndpoint,
        schema_validator: SchemaValidator,
        *,
        event_sink: EventSink | None = None,
    ) -> None:
        self._settings = settings
        self._descriptor = descriptor
        self._endpoint = endpoint
        self._protocol_version = endpoint.protocol_version
        if (
            not isinstance(self._protocol_version, str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", self._protocol_version) is None
        ):
            raise McpProtocolError("invalid MCP negotiated protocol version")
        self._validator = schema_validator
        self._sink = ensure_isolated_event_sink(event_sink or NullEventSink())
        limits = settings.limits
        try:
            encoded = _json(
                dict(descriptor.input_schema),
                max_bytes=limits.max_schema_bytes,
                max_depth=limits.max_depth,
            )
            output = (
                None
                if descriptor.output_schema is None
                else _json(
                    dict(descriptor.output_schema),
                    max_bytes=limits.max_schema_bytes,
                    max_depth=limits.max_depth,
                )
            )
        except (ValueError, TypeError, RecursionError) as error:
            raise ConfigurationError("MCP schema exceeds supported budgets") from error
        self._input: JSONObject = json.loads(encoded)
        self.schema_digest = hashlib.sha256(
            (encoded + "\n" + (output or "")).encode()
        ).hexdigest()
        self._output = (
            None
            if output is None
            else _output_wrapper(json.loads(output), self.schema_digest)
        )
        if (
            self._input.get("type") != "object"
            or not schema_validator.supports(deepcopy(self._input))
            or (
                self._output is not None
                and not schema_validator.supports(deepcopy(self._output))
            )
        ):
            raise ConfigurationError(
                "MCP tool requires a supported input/output schema"
            )
        self._definition = ToolDefinition(
            mcp_tool_name(settings.spec.server_id, descriptor.remote_name),
            (descriptor.description.strip() or "Call the selected remote MCP tool")[
                :4096
            ],
            descriptor.input_schema,
            side_effects="external",
            requires_approval=True,
            retry_safe=False,
            replay_safe=False,
            timeout_seconds=settings.call_timeout_seconds
            + settings.cleanup_grace_seconds,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def _metadata(
        self, *, outcome: str | None = None, present: bool = False
    ) -> JSONObject:
        value: JSONObject = {
            "server_id": self._settings.spec.server_id,
            "remote_tool": self._descriptor.remote_name,
            "schema_digest": self.schema_digest,
            "protocol_version": self._protocol_version,
            "has_structured_content": present,
        }
        if outcome is not None:
            value["outcome"] = outcome
        return value

    def _unknown(self) -> ToolOutput:
        return ToolOutput(
            "MCP tool outcome or result is unknown; do not retry automatically.",
            {"mcp": self._metadata(outcome="outcome_unknown"), "result": None},
            True,
        )

    def _project(self, result: McpCallResult) -> ToolOutput:
        if not isinstance(result, McpCallResult) or result.result_type != "result":
            raise McpResultError("unsupported MCP result type")
        limits = self._settings.limits
        value = result.structured_content
        encoded = (
            _json(
                value, max_bytes=limits.max_structured_bytes, max_depth=limits.max_depth
            )
            if result.has_structured_content
            else ""
        )
        _json(
            {
                "content": [{"type": b.kind, "text": b.text} for b in result.content],
                "result": value,
            },
            max_bytes=limits.max_result_bytes,
            max_depth=limits.max_depth + 2,
        )
        if self._output is not None and not result.is_error:
            if not result.has_structured_content:
                raise McpResultError("MCP success omitted required structured content")
            self._validator.validate(
                deepcopy(self._output), {"result": deepcopy(value)}
            )
        parts, representable = [], result.has_structured_content
        for block in result.content:
            if block.kind == "text":
                parts.append(block.text)
                representable = True
            else:
                parts.append(f"[MCP {block.kind} content omitted]")
        if not representable:
            raise McpResultError("MCP result has no representable content")
        if result.has_structured_content and not any(
            b.kind == "text" for b in result.content
        ):
            parts.append(encoded)
        content = "\n\n".join(parts)
        if len(content) > limits.max_content_chars:
            raise McpResultError("MCP result exceeds content budget")
        metadata = self._metadata(present=result.has_structured_content)
        metadata["omitted_content_blocks"] = sum(
            b.kind != "text" for b in result.content
        )
        return ToolOutput(content, {"mcp": metadata, "result": value}, result.is_error)

    async def execute(
        self, arguments: JSONObject, context: ToolExecutionContext
    ) -> ToolOutput:
        # Defensive validation also covers direct users of the Tool port.
        snapshot = deepcopy(arguments)
        try:
            _json(
                snapshot,
                max_bytes=self._settings.limits.max_schema_bytes,
                max_depth=self._settings.limits.max_depth,
            )
        except (TypeError, ValueError, RecursionError) as error:
            raise ToolValidationError(
                ("$: MCP arguments exceed JSON budgets",)
            ) from error
        self._validator.validate(deepcopy(self._input), deepcopy(snapshot))
        invocation = uuid4().hex
        started = time.monotonic()

        def emit(
            kind: EventKind,
            outcome: str | None = None,
            error: BaseException | None = None,
        ) -> None:
            self._sink.emit(
                make_agent_event(
                    kind,
                    "capability",
                    source="mcp",
                    session_id=context.session_id,
                    run_id=context.run_id,
                    turn_id=context.turn_id,
                    span_id=invocation,
                    duration_ms=None
                    if outcome is None
                    else (time.monotonic() - started) * 1000,
                    payload={
                        "server_id": self._settings.spec.server_id,
                        "invocation_id": invocation,
                        "tool_call_id": context.call.id,
                        "outcome": outcome,
                        "error_type": None if error is None else type(error).__name__,
                    },
                )
            )

        emit("mcp_request_started")
        try:
            async with asyncio.timeout(self._settings.call_timeout_seconds):
                result = await self._endpoint.call_tool(
                    self._descriptor.remote_name, snapshot
                )
            output = self._project(result)
        except asyncio.CancelledError:
            emit("mcp_request_cancelled", "cancelled")
            raise
        except McpRequestNotStartedError as error:
            emit("mcp_request_failed", "error", error)
            return ToolOutput(
                "MCP request was not sent because its connection or queue is unavailable.",
                {"mcp": self._metadata(outcome="not_started"), "result": None},
                True,
            )
        except (
            McpConnectionError,
            McpProtocolError,
            McpTransportError,
            McpResultError,
            ToolValidationError,
            TimeoutError,
            ValueError,
            TypeError,
            RecursionError,
        ) as error:
            emit(
                "mcp_request_failed",
                "timeout" if isinstance(error, TimeoutError) else "error",
                error,
            )
            return self._unknown()
        except BaseException as error:
            emit("mcp_request_failed", "error", error)
            raise
        emit("mcp_request_finished", "error" if output.is_error else "success")
        return output
