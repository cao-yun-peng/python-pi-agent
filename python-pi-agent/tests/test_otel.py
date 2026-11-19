from __future__ import annotations

import unittest

from pi_agent_py.api import (
    AgentEventEnvelope,
    AgentRuntimeBuilder,
    EventKind,
    Finish,
    FunctionTool,
    JSONObject,
    TelemetryEvent,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from pi_agent_py.testing import FakeProvider

try:
    from opentelemetry.sdk.trace import TracerProvider
except ModuleNotFoundError as exc:
    if exc.name not in {
        "opentelemetry",
        "opentelemetry.sdk",
        "opentelemetry.sdk.trace",
    }:
        raise
    raise unittest.SkipTest("install the otel extra") from exc

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pi_agent_py.observability.otel import OtelTelemetrySink


class OtelTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_connection_and_request_spans_pair_without_remote_content(self):
        import asyncio
        import importlib.util

        if importlib.util.find_spec("jsonschema") is None:
            self.skipTest("install [jsonschema]")
        from pi_agent_py.api import DeclaredSideEffectGate, JsonSchemaValidator, StaticApprovalPolicy
        from pi_agent_py.testing import FakeMcpConnector, FakeMcpEndpoint, WaitUntilCancelled
        from pi_agent_py.tools.mcp.contracts import (
            AllowedMcpConnections, McpCallResult, McpConnectionSpec, McpContent,
            McpServerSettings, McpToolDescriptor, McpTransportError,
        )
        from pi_agent_py.tools.mcp.plugin import mcp_plugin
        from pi_agent_py.tools.mcp.tool import mcp_tool_name

        for mode in ("success", "failure", "cancelled"):
            spec = McpConnectionSpec("private-server", "stdio", command="unused")
            endpoint = FakeMcpEndpoint((McpToolDescriptor("private-tool", "Private", {"type": "object"}),),
                {"success": [McpCallResult((McpContent("text", "private-result"),))],
                 "failure": [McpTransportError("private-error")],
                 "cancelled": [WaitUntilCancelled()]}[mode])
            validator = JsonSchemaValidator()
            provider = FakeProvider([
                [ToolCall("c", mcp_tool_name(spec.server_id, "private-tool"), {}), Finish("tool_calls")],
                [Finish()],
            ])
            runtime = await (AgentRuntimeBuilder().with_provider(provider)
                .with_schema_validator(validator).with_approval_policy(StaticApprovalPolicy(True))
                .with_execution_gate(DeclaredSideEffectGate(allowed_side_effects=("external",)))
                .with_telemetry_sink(self.sink)
                .with_plugin("mcp", mcp_plugin((McpServerSettings(spec, ("private-tool",)),),
                    connector=FakeMcpConnector({spec.server_id: endpoint}), schema_validator=validator,
                    connection_policy=AllowedMcpConnections((spec,)))).build())
            async with runtime:
                task = asyncio.create_task(runtime.run(mode, "private-prompt"))
                if mode == "cancelled":
                    await endpoint.blocked.wait()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    await task
        spans = self.exporter.get_finished_spans()
        connections = [s for s in spans if s.name == "pi_agent.mcp_connection"]
        requests = [s for s in spans if s.name == "pi_agent.mcp_request"]
        self.assertEqual(len(connections), 3)
        self.assertEqual([s.attributes["pi_agent.outcome"] for s in requests], ["success", "error", "cancelled"])
        self.assertTrue(all(s.end_time >= s.start_time for s in connections + requests))
        self.assertNotIn("private", repr([dict(s.attributes) for s in connections + requests]))

    async def test_middleware_spans_cover_success_failure_and_cancellation(self) -> None:
        from pi_agent_py.api import Middleware, MiddlewareError
        import asyncio

        for mode in ("success", "failure", "cancelled"):
            started = asyncio.Event()
            class Hook:
                async def prepare(self, context, plan):
                    if mode == "failure":
                        raise ValueError("private-error")
                    if mode == "cancelled":
                        started.set()
                        await asyncio.Event().wait()
                    return plan
            runtime = await (AgentRuntimeBuilder().with_provider(FakeProvider([[Finish()]]))
                             .with_middleware((Middleware("test", provider_request=Hook()),))
                             .with_telemetry_sink(self.sink).build())
            async with runtime:
                task = asyncio.create_task(runtime.run(mode, "private prompt"))
                if mode == "cancelled":
                    await started.wait()
                    task.cancel()
                if mode == "success":
                    await task
                else:
                    with self.assertRaises(asyncio.CancelledError if mode == "cancelled" else MiddlewareError):
                        await task
        spans = [s for s in self.exporter.get_finished_spans() if s.name == "pi_agent.middleware"]
        self.assertEqual(len(spans), 3)
        self.assertEqual([s.attributes["pi_agent.outcome"] for s in spans], ["success", "error", "cancelled"])
        self.assertTrue(all(s.end_time >= s.start_time for s in spans))
        self.assertNotIn("private", repr([dict(s.attributes) for s in spans]))

    def setUp(self) -> None:
        self.exporter = InMemorySpanExporter()
        traces = TracerProvider()
        traces.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.reader = InMemoryMetricReader()
        metrics = MeterProvider(metric_readers=[self.reader])
        self.addCleanup(traces.shutdown)
        self.addCleanup(metrics.shutdown)
        self.sink = OtelTelemetrySink(
            traces.get_tracer("test"),
            metrics.get_meter("test"),
            tool_names=frozenset({"tool"}),
            provider_names=frozenset({"fake"}),
        )

    async def test_runtime_parentage_and_metrics(self) -> None:
        async def tool(
            arguments: JSONObject, context: ToolExecutionContext
        ) -> ToolOutput:
            return ToolOutput("private response", is_error=True)

        provider = FakeProvider(
            [
                [
                    ToolCall("call", "tool", {"secret": "private arguments"}),
                    Finish("tool_calls"),
                ],
                [TokenUsage(8, 3, 11, 2), Finish()],
            ]
        )
        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(provider, name="fake")
            .with_tools(
                [
                    FunctionTool(
                        ToolDefinition("tool", "Test tool", {"type": "object"}), tool
                    )
                ]
            )
            .with_telemetry_sink(self.sink, ownership="owned")
            .build()
        )
        async with runtime:
            await runtime.run("s", "private prompt")
        spans = self.exporter.get_finished_spans()
        (root,) = [span for span in spans if span.name == "pi_agent.run"]
        turns = {span.context.span_id for span in spans if span.name == "pi_agent.turn"}
        requests = {
            span.context.span_id
            for span in spans
            if span.name == "pi_agent.model_request"
        }
        self.assertEqual(len(turns), 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(root.attributes["pi_agent.session_id"], "s")
        self.assertEqual(
            root.attributes["pi_agent.run_id"], provider.request_contexts[0].run_id
        )
        for span in spans:
            if span.name == "pi_agent.turn":
                self.assertEqual(span.parent.span_id, root.context.span_id)
            elif span.name in ("pi_agent.model_request", "pi_agent.tool"):
                self.assertIn(span.parent.span_id, turns)
            elif span.name == "pi_agent.provider_attempt":
                self.assertIn(span.parent.span_id, requests)
        self.assertNotIn("private", repr([dict(span.attributes) for span in spans]))
        data = self.reader.get_metrics_data()
        series = {
            metric.name: metric.data.data_points
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        self.assertEqual(series["pi_agent.model.request.duration"][0].count, 2)
        self.assertEqual(
            {p.attributes["type"]: p.value for p in series["pi_agent.tokens"]},
            {"input": 8, "output": 3, "cached": 2},
        )
        self.assertEqual(
            series["pi_agent.tool.executions"][0].attributes,
            {"tool": "tool", "result": "error"},
        )
        self.assertEqual(series["pi_agent.provider.attempts"][0].value, 2)

    async def test_exact_duration_and_settled_closes_unmatched_child(self) -> None:
        def emit(
            kind: EventKind,
            when: float,
            *,
            span_id: str | None = None,
            duration_ms: float | None = None,
        ) -> None:
            self.sink.on_event(
                TelemetryEvent(
                    kind,
                    "live",
                    AgentEventEnvelope(
                        occurred_at=when,
                        session_id="s",
                        run_id="r",
                        turn_id="t" if span_id is not None else None,
                        span_id=span_id,
                        duration_ms=duration_ms,
                    ),
                )
            )

        emit("run_started", 1)
        emit("model_request_started", 2, span_id="req")
        emit("model_request_finished", 3, span_id="req", duration_ms=500)
        emit("plugin_hook_started", 4, span_id="hook")
        emit("run_cancelled", 5)
        emit("run_settled", 6)
        spans = {span.name: span for span in self.exporter.get_finished_spans()}
        self.assertEqual(spans["pi_agent.model_request"].end_time, 2_500_000_000)
        self.assertEqual(spans["pi_agent.hook"].end_time, 6_000_000_000)
        self.assertEqual(
            spans["pi_agent.hook"].attributes["pi_agent.outcome"], "cancelled"
        )
        self.assertEqual(
            spans["pi_agent.hook"].parent.span_id,
            spans["pi_agent.turn"].context.span_id,
        )
        await self.sink.aclose()
