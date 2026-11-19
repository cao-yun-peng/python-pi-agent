"""Offline MCP adapter contracts: ordinary Tool pipeline, no SDK or server."""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    CallbackEventSink,
    DeclaredSideEffectGate,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    InMemorySessionStore,
    JsonSchemaValidator,
    Middleware,
    RuntimeBuildError,
    JsonlSessionStore,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolOutput,
    TruncateToolResults,
)
from pi_agent_py.errors import ConfigurationError, PluginLoadError
from pi_agent_py.plugins import PluginHost
from pi_agent_py.testing import (
    FakeMcpConnector,
    FakeMcpEndpoint,
    FakeProvider,
    WaitUntilCancelled,
)
from pi_agent_py.tools.mcp.contracts import (
    AllowedMcpConnections,
    McpCallResult,
    McpConnectionSpec,
    McpContent,
    McpLimits,
    McpServerSettings,
    McpToolDescriptor,
    McpTransportError,
)
from pi_agent_py.tools.mcp.plugin import mcp_plugin
from pi_agent_py.tools.mcp.tool import McpTool, mcp_tool_name


SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string", "pattern": "^OK-"}},
    "required": ["code"],
    "additionalProperties": False,
}


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "install [jsonschema]")
class McpTests(unittest.IsolatedAsyncioTestCase):
    def setup_adapter(self, responses=(), *, output_schema=None, **endpoint_options):
        self.spec = McpConnectionSpec("notes", "stdio", command="unused-fake-server")
        self.descriptor = McpToolDescriptor(
            "notes/search",
            "Search notes",
            SCHEMA,
            output_schema,
            {"readOnlyHint": True},
        )
        self.endpoint = FakeMcpEndpoint(
            (self.descriptor,), responses, **endpoint_options
        )
        self.connector = FakeMcpConnector({"notes": self.endpoint})
        self.validator = JsonSchemaValidator()
        self.settings = McpServerSettings(self.spec, ("notes/search",))
        self.name = mcp_tool_name("notes", "notes/search")

    def builder(
        self, *, arguments=None, approval=True, gate=True, policy=True, settings=None
    ):
        self.provider = FakeProvider(
            [
                [
                    ToolCall(
                        "c",
                        self.name,
                        {"code": "OK-1"} if arguments is None else arguments,
                    ),
                    Finish("tool_calls"),
                ],
                [TextDelta("done"), Finish()],
            ]
        )
        self.events = InMemoryEventSink()
        self.store = InMemorySessionStore()
        self.approval = StaticApprovalPolicy(bool(approval))
        builder = (
            AgentRuntimeBuilder()
            .with_provider(self.provider)
            .with_schema_validator(self.validator)
            .with_session_store(self.store)
            .with_event_sink(self.events)
            .with_plugin(
                "mcp",
                mcp_plugin(
                    (settings or self.settings,),
                    connector=self.connector,
                    schema_validator=self.validator,
                    connection_policy=AllowedMcpConnections((self.spec,))
                    if policy
                    else None,
                ),
            )
        )
        if approval is not None:
            builder.with_approval_policy(self.approval)
        if gate:
            builder.with_execution_gate(
                DeclaredSideEffectGate(allowed_side_effects=("external",))
            )
        return builder

    async def test_complete_pipeline_and_offline_fork(self):
        self.setup_adapter(
            [
                McpCallResult(
                    (McpContent("text", "x" * 200),),
                    structured_content={"count": 2},
                    has_structured_content=True,
                )
            ]
        )
        runtime = (
            await self.builder()
            .with_middleware(
                (Middleware("truncate", tool_result=TruncateToolResults(64)),)
            )
            .build()
        )
        async with runtime:
            result = await runtime.run("s", "search")
            self.assertEqual(result.message.content, "done")
        output = (await self.store.load("s")).messages[2]
        self.assertEqual(len(output.content), 64)
        self.assertEqual(self.provider.requests[1][2], output)
        self.assertEqual(output.structured_content["result"], {"count": 2})
        self.assertEqual(
            output.structured_content["mcp"]["remote_tool"], "notes/search"
        )
        self.assertEqual((await self.store.fork("s", "fork")).messages[2], output)
        self.assertEqual(self.endpoint.calls, [("notes/search", {"code": "OK-1"})])
        self.assertEqual(self.endpoint.close_count, 1)
        request = [e for e in self.events.events if e.kind.startswith("mcp_request_")]
        self.assertEqual(
            [e.kind for e in request], ["mcp_request_started", "mcp_request_finished"]
        )
        self.assertEqual(request[0].envelope.span_id, request[1].envelope.span_id)
        self.assertIsNotNone(request[0].envelope.run_id)
        self.assertEqual(request[0].envelope.session_id, "s")
        self.assertNotIn("OK-1", repr(request))

    async def test_binding_ignores_remote_safety_hints(self):
        self.setup_adapter()
        binding = McpTool(self.settings, self.descriptor, self.endpoint, self.validator)
        definition = binding.definition
        self.assertEqual(definition.side_effects, "external")
        self.assertTrue(definition.requires_approval)
        self.assertFalse(definition.retry_safe)
        self.assertFalse(definition.replay_safe)
        self.assertGreater(
            definition.timeout_seconds, self.settings.call_timeout_seconds
        )

    async def test_validation_approval_and_gate_each_prevent_remote_call(self):
        for options, reviews in (
            ({"arguments": {"code": "secret"}}, 0),
            ({"approval": None}, 0),
            ({"approval": False}, 1),
            ({"gate": False}, 1),
        ):
            self.setup_adapter()
            async with await self.builder(**options).build() as runtime:
                result = await runtime.run("s", "q")
            self.assertEqual(self.endpoint.calls, [])
            self.assertTrue(result.messages[2].is_error)
            self.assertEqual(len(self.approval.requests), reviews)

    async def test_default_connection_policy_denies_before_open(self):
        self.setup_adapter()
        with self.assertRaises(RuntimeBuildError):
            await self.builder(policy=False).build()
        self.assertEqual(self.connector.opens, [])

    async def test_missing_selection_rolls_back_open_connection(self):
        self.setup_adapter()
        settings = McpServerSettings(self.spec, ("missing",))
        with self.assertRaises(RuntimeBuildError):
            await self.builder(settings=settings).build()
        self.assertEqual(self.endpoint.close_count, 1)

    async def test_remote_failure_and_zero_deadline_never_retry(self):
        for response, timeout in (
            (McpTransportError("secret diagnostic"), 30),
            (WaitUntilCancelled(), 0),
        ):
            self.setup_adapter([response])
            settings = McpServerSettings(
                self.spec, ("notes/search",), call_timeout_seconds=timeout
            )
            async with await self.builder(settings=settings).build() as runtime:
                result = await runtime.run("s", "q")
            output = result.messages[2]
            self.assertTrue(output.is_error)
            self.assertIn("do not retry automatically", output.content)
            self.assertNotIn("secret", output.content)
            self.assertEqual(
                output.structured_content["mcp"]["outcome"], "outcome_unknown"
            )
            self.assertEqual(len(self.endpoint.calls), 1)

    async def test_cancel_propagates_without_durable_result_and_closes(self):
        self.setup_adapter([WaitUntilCancelled()])
        runtime = await self.builder().build()
        task = asyncio.create_task(runtime.run("s", "q"))
        await self.endpoint.blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(
            any(m.role == "tool" for m in (await self.store.load("s")).messages)
        )
        await runtime.aclose()
        self.assertEqual(self.endpoint.close_count, 1)
        self.assertIn("mcp_request_cancelled", [e.kind for e in self.events.events])
        # Recovery/fork need neither a connector nor a live server and do not resend.
        async with (
            await AgentRuntimeBuilder()
            .with_provider(FakeProvider([[Finish()]]))
            .with_session_store(self.store)
            .build() as recovered
        ):
            await recovered.run("s", "continue")
        tool_results = [
            m for m in (await self.store.load("s")).messages if m.role == "tool"
        ]
        self.assertEqual(len(tool_results), 1)
        self.assertTrue(tool_results[0].is_error)
        self.assertEqual(len(self.endpoint.calls), 1)

    async def test_projection_preserves_presence_order_errors_and_omissions(self):
        cases = [
            (
                McpCallResult(
                    (McpContent("image"),),
                    structured_content={"count": 1},
                    has_structured_content=True,
                ),
                '[MCP image content omitted]\n\n{"count":1}',
                False,
                True,
            ),
            (
                McpCallResult(
                    (
                        McpContent("text", "one"),
                        McpContent("image"),
                        McpContent("text", "two"),
                    )
                ),
                "one\n\n[MCP image content omitted]\n\ntwo",
                False,
                False,
            ),
            (McpCallResult(has_structured_content=True), "null", False, True),
            (
                McpCallResult(structured_content=[2, 1], has_structured_content=True),
                "[2,1]",
                False,
                True,
            ),
            (
                McpCallResult(
                    (McpContent("text", "remote business failure"),), is_error=True
                ),
                "remote business failure",
                True,
                False,
            ),
        ]
        for response, content, error, present in cases:
            self.setup_adapter([response])
            async with await self.builder().build() as runtime:
                output = (await runtime.run("s", "q")).messages[2]
            self.assertEqual(output.content, content)
            self.assertEqual(output.is_error, error)
            self.assertEqual(
                output.structured_content["mcp"]["has_structured_content"], present
            )

        # Missing structured content does not consume a structured-value budget.
        self.setup_adapter([McpCallResult((McpContent("text", "text only"),))])
        settings = replace(self.settings, limits=McpLimits(max_structured_bytes=1))
        async with await self.builder(settings=settings).build() as runtime:
            self.assertEqual(
                (await runtime.run("s", "q")).messages[2].content, "text only"
            )

    async def test_output_schema_checks_original_value_with_local_refs(self):
        schema = {
            "$defs": {"value": {"type": ["integer", "null"]}},
            "$ref": "#/$defs/value",
        }
        for value, present, error, expected in (
            (3, True, False, False),
            (None, True, False, False),
            ("wrong", True, False, True),
            (None, False, False, True),
            ("business-error", True, True, True),
        ):
            self.setup_adapter(
                [
                    McpCallResult(
                        structured_content=value,
                        has_structured_content=present,
                        is_error=error,
                    )
                ],
                output_schema=schema,
            )
            async with await self.builder().build() as runtime:
                output = (await runtime.run("s", "q")).messages[2]
            self.assertEqual(output.is_error, expected)
            if present and not (expected and not error):
                self.assertEqual(output.structured_content["result"], value)

    async def test_unrepresentable_or_over_budget_result_is_unknown_once(self):
        cases = [
            (McpCallResult((McpContent("image"),)), McpLimits()),
            (McpCallResult(result_type="input_required"), McpLimits()),
            (
                McpCallResult((McpContent("text", "too long"),)),
                McpLimits(max_content_chars=2),
            ),
            (
                McpCallResult(
                    structured_content={"secret": "large"}, has_structured_content=True
                ),
                McpLimits(max_structured_bytes=2),
            ),
            (
                McpCallResult((McpContent("text", "payload"),)),
                McpLimits(max_result_bytes=2),
            ),
            (
                McpCallResult(
                    structured_content={"nested": [[1]]}, has_structured_content=True
                ),
                McpLimits(max_depth=1),
            ),
        ]
        for response, limits in cases:
            self.setup_adapter([response])
            # Keep discovery within budget while making the result exceed it.
            if limits.max_depth == 1:
                limits = replace(limits, max_depth=4)
                response = McpCallResult(
                    structured_content=[[[[[1]]]]], has_structured_content=True
                )
                self.endpoint = FakeMcpEndpoint((self.descriptor,), (response,))
                self.connector = FakeMcpConnector({"notes": self.endpoint})
            settings = replace(self.settings, limits=limits)
            async with await self.builder(settings=settings).build() as runtime:
                output = (await runtime.run("s", "q")).messages[2]
            self.assertTrue(output.is_error)
            self.assertEqual(
                output.structured_content["mcp"]["outcome"], "outcome_unknown"
            )
            self.assertEqual(len(self.endpoint.calls), 1)
            self.assertNotIn("secret", output.content)

    async def test_discovery_contract_failures_close_without_publishing(self):
        self.setup_adapter()
        cases = [
            (self.descriptor, self.descriptor),
            (
                replace(
                    self.descriptor,
                    input_schema={"type": "object", "x-unsupported": True},
                ),
            ),
            (
                replace(
                    self.descriptor,
                    input_schema={
                        "type": "object",
                        "$ref": "https://invalid.test/schema",
                    },
                ),
            ),
            (replace(self.descriptor, output_schema={"type": "invalid"}),),
        ]
        for descriptors in cases:
            endpoint = FakeMcpEndpoint(descriptors)
            host = PluginHost()
            with self.assertRaises(PluginLoadError):
                await host.load(
                    "mcp",
                    mcp_plugin(
                        (self.settings,),
                        connector=FakeMcpConnector({"notes": endpoint}),
                        schema_validator=self.validator,
                        connection_policy=AllowedMcpConnections((self.spec,)),
                    ),
                )
            self.assertEqual(host.snapshot().tools, ())
            self.assertEqual(endpoint.close_count, 1)

    async def test_discovery_and_schema_budgets_are_enforced(self):
        for limits in (
            McpLimits(max_discovered_tools=1),
            McpLimits(max_catalog_bytes=10),
            McpLimits(max_schema_bytes=10),
        ):
            self.setup_adapter()
            if limits.max_discovered_tools == 1:
                self.endpoint = FakeMcpEndpoint(
                    (self.descriptor, replace(self.descriptor, remote_name="extra"))
                )
                self.connector = FakeMcpConnector({"notes": self.endpoint})
            with self.assertRaises(RuntimeBuildError):
                await self.builder(
                    settings=replace(self.settings, limits=limits)
                ).build()
            self.assertEqual(self.endpoint.close_count, 1)

    async def test_required_servers_fail_transactionally_and_close_in_reverse_order(
        self,
    ):
        self.setup_adapter()
        order = []

        class Endpoint(FakeMcpEndpoint):
            async def aclose(inner):
                order.append(inner.label)
                await super().aclose()

        servers, endpoints = [], {}
        for name in ("one", "two"):
            spec = replace(self.spec, server_id=name)
            servers.append(McpServerSettings(spec, ("notes/search",)))
            endpoint = Endpoint(
                (self.descriptor,),
                list_error=ValueError("private") if name == "two" else None,
            )
            endpoint.label = name
            endpoints[name] = endpoint
        host = PluginHost()
        with self.assertRaises(PluginLoadError):
            await host.load(
                "mcp",
                mcp_plugin(
                    servers,
                    connector=FakeMcpConnector(endpoints),
                    schema_validator=self.validator,
                    connection_policy=AllowedMcpConnections(
                        tuple(s.spec for s in servers)
                    ),
                ),
            )
        self.assertEqual(order, ["two", "one"])
        self.assertEqual(host.snapshot().tools, ())

    async def test_existing_name_and_digest_collision_roll_back(self):
        self.setup_adapter()

        async def unused(arguments, context):
            return ToolOutput("unused")

        local = FunctionTool(
            ToolDefinition(self.name, "Local", {"type": "object"}), unused
        )
        with self.assertRaises(RuntimeBuildError):
            await self.builder().with_tools((local,)).build()
        self.assertEqual(self.endpoint.close_count, 1)
        self.setup_adapter()
        extra = replace(self.descriptor, remote_name="another")
        self.endpoint = FakeMcpEndpoint((self.descriptor, extra))
        self.connector = FakeMcpConnector({"notes": self.endpoint})
        with patch(
            "pi_agent_py.tools.mcp.tool.mcp_tool_name", return_value="collision"
        ):
            with self.assertRaises(RuntimeBuildError):
                await self.builder(
                    settings=replace(
                        self.settings, allow_tools=("notes/search", "another")
                    )
                ).build()
        self.assertEqual(self.endpoint.close_count, 1)

    async def test_cancel_during_discovery_rolls_back_and_pairs_span(self):
        self.setup_adapter(list_error=WaitUntilCancelled())
        task = asyncio.create_task(self.builder().build())
        await self.endpoint.discovery_blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.endpoint.close_count, 1)
        events = [
            e
            for e in self.events.events
            if e.kind in {"mcp_connection_started", "mcp_connection_cancelled"}
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].envelope.span_id, events[1].envelope.span_id)

    async def test_two_sessions_share_endpoint_but_cancellation_is_local(self):
        self.setup_adapter(
            [WaitUntilCancelled(), McpCallResult((McpContent("text", "second"),))]
        )
        builder = self.builder()
        provider = FakeProvider(
            [
                [ToolCall("same", self.name, {"code": "OK-1"}), Finish("tool_calls")],
                [ToolCall("same", self.name, {"code": "OK-2"}), Finish("tool_calls")],
                [Finish()],
            ]
        )
        async with await builder.with_provider(provider).build() as runtime:
            first = asyncio.create_task(runtime.run("one", "q"))
            await self.endpoint.blocked.wait()
            second = asyncio.create_task(runtime.run("two", "q"))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            result = await second
        self.assertEqual(result.messages[2].content, "second")
        self.assertEqual(len(self.endpoint.calls), 2)
        events = [e for e in self.events.events if e.kind == "mcp_request_started"]
        self.assertEqual({e.envelope.session_id for e in events}, {"one", "two"})
        self.assertEqual(len({e.envelope.span_id for e in events}), 2)

    async def test_jsonl_contains_transformed_result_and_reopens_without_mcp(self):
        self.setup_adapter(
            [McpCallResult((McpContent("text", "uncommitted original"),))]
        )

        class Redact:
            async def transform(self, context, output):
                return ToolOutput("redacted", {"safe": True}, output.is_error)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            runtime = await (
                self.builder()
                .with_session_store(JsonlSessionStore(path))
                .with_middleware((Middleware("redact", tool_result=Redact()),))
                .build()
            )
            async with runtime:
                await runtime.run("s", "q")
            self.assertNotIn("uncommitted original", path.read_text(encoding="utf-8"))
            snapshot = await JsonlSessionStore(path).load("s")
            self.assertEqual(snapshot.messages[2].content, "redacted")
            self.assertEqual(len(self.endpoint.calls), 1)

    async def test_broken_observer_does_not_change_execution(self):
        self.setup_adapter([McpCallResult((McpContent("text", "ok"),))])

        def broken(event):
            raise ValueError("private observer failure")

        async with (
            await self.builder()
            .with_event_sink(CallbackEventSink(broken))
            .build() as runtime
        ):
            self.assertEqual((await runtime.run("s", "q")).messages[2].content, "ok")


class McpValueTests(unittest.TestCase):
    def test_names_are_bounded_stable_and_distinguish_normalized_collisions(self):
        names = ["a/b", "a:b", "同名", "x" * 500]
        result = {name: mcp_tool_name("server", name) for name in names}
        self.assertEqual(len(set(result.values())), len(names))
        self.assertEqual(
            result, {name: mcp_tool_name("server", name) for name in reversed(names)}
        )
        for name in result.values():
            self.assertLessEqual(len(name), 64)
            self.assertRegex(name, r"^[A-Za-z0-9_-]+$")

    def test_values_detach_nested_data_and_hide_credentials(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        descriptor = McpToolDescriptor("tool", "", schema)
        schema["properties"].clear()
        exposed = descriptor.input_schema["properties"]
        exposed.clear()
        self.assertIn("x", descriptor.input_schema["properties"])
        value = {"items": [1]}
        result = McpCallResult(structured_content=value, has_structured_content=True)
        value["items"].append(2)
        result.structured_content["items"].append(3)
        self.assertEqual(result.structured_content, {"items": [1]})
        spec = McpConnectionSpec(
            "remote",
            "streamable_http",
            url="https://example.test/mcp?secret=1",
            headers={"Authorization": "private"},
        )
        self.assertNotIn("private", repr(spec))
        self.assertNotIn("secret", repr(spec))
        with self.assertRaises(ConfigurationError):
            McpConnectionSpec(
                "bad", "streamable_http", url="https://user:password@example.test/mcp"
            )

    def test_invalid_settings_and_nonfinite_results_are_rejected(self):
        spec = McpConnectionSpec("fake", "stdio", command="unused")
        for names in ((), ("a", "a")):
            with self.assertRaises(ConfigurationError):
                McpServerSettings(spec, names)
        with self.assertRaises(ConfigurationError):
            McpServerSettings(spec, ("a",), call_timeout_seconds=float("inf"))
        from pi_agent_py.tools.mcp.contracts import McpResultError

        with self.assertRaises(McpResultError):
            McpCallResult(structured_content=float("nan"), has_structured_content=True)


if __name__ == "__main__":
    unittest.main()
