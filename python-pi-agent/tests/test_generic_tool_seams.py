"""Contracts required before a transport-specific tool adapter is introduced."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest

from pi_agent_py.api import (
    Agent,
    AgentComponents,
    AgentProfile,
    AgentRuntimeBuilder,
    Finish,
    InMemoryEventSink,
    InternalError,
    PluginAPI,
    StaticApprovalPolicy,
    SubsetSchemaValidator,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    ToolValidationError,
    FunctionTool,
)
from pi_agent_py.observability.events import make_agent_event
from pi_agent_py.testing import FakeProvider


class RecordingValidator(SubsetSchemaValidator):
    def __init__(self, trace):
        self.trace = trace

    def validate(self, schema, arguments):
        self.trace.append("validate")
        super().validate(schema, arguments)


class GenericToolSeamsTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_validator_return_cannot_bypass_validation(self):
        class Wrong(SubsetSchemaValidator):
            def validate(self, schema, arguments):
                return False

        class Async(SubsetSchemaValidator):
            async def validate(self, schema, arguments):
                pass

        async def execute(arguments, context):
            self.fail("must not execute")

        for validator in (Wrong(), Async()):
            agent = Agent(
                AgentComponents(
                    FakeProvider([[ToolCall("c", "echo", {}), Finish("tool_calls")]]),
                    tools=(
                        FunctionTool(
                            ToolDefinition("echo", "Echo", {"type": "object"}), execute
                        ),
                    ),
                    schema_validator=validator,
                )
            )
            with self.assertRaises(InternalError) as caught:
                await agent.run("q")
            self.assertIsInstance(caught.exception.__cause__, TypeError)

    async def test_context_identity_survives_worker_tasks_and_multiple_turns(self):
        from pi_agent_py.api import Middleware

        seen, results = [], []

        async def worker(context):
            await asyncio.sleep(0)
            seen.append((context.run_id, context.turn_id, context.request_number))
            return ToolOutput("ok")

        async def execute(arguments, context):
            return await asyncio.create_task(worker(context))

        class ResultHook:
            async def transform(self, context, output):
                results.append(
                    (
                        context.run_id,
                        context.turn_id,
                        context.execution.run_id,
                        context.execution.turn_id,
                    )
                )
                return output

        provider = FakeProvider(
            [
                [ToolCall("same-id", "echo", {}), Finish("tool_calls")],
                [ToolCall("same-id", "echo", {}), Finish("tool_calls")],
                [Finish()],
                [ToolCall("same-id", "echo", {}), Finish("tool_calls")],
                [Finish()],
            ]
        )
        agent = Agent(
            AgentComponents(
                provider,
                tools=(
                    FunctionTool(
                        ToolDefinition("echo", "Echo", {"type": "object"}), execute
                    ),
                ),
                middleware=(Middleware("identity", tool_result=ResultHook()),),
            )
        )
        await agent.run("one", run_id="r1")
        await agent.run("two", run_id="r2")
        self.assertEqual(
            seen,
            [("r1", "r1:turn:1", 1), ("r1", "r1:turn:2", 2), ("r2", "r2:turn:1", 1)],
        )
        self.assertTrue(
            all(
                run == inner_run and turn == inner_turn
                for run, turn, inner_run, inner_turn in results
            )
        )

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "install [jsonschema]")
    async def test_full_schema_is_checked_before_approval(self):
        from pi_agent_py.api import JsonSchemaValidator

        schema = {
            "type": "object",
            "properties": {
                "code": {
                    "anyOf": [
                        {"type": "string", "pattern": "^OK-[0-9]+$"},
                        {"type": "null"},
                    ],
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        }
        policy, calls = StaticApprovalPolicy(True), []

        async def execute(arguments, context):
            calls.append(arguments)
            return ToolOutput("accepted")

        definition = ToolDefinition(
            "check", "Check a code", schema, requires_approval=True
        )
        provider = FakeProvider(
            [
                [
                    ToolCall("bad", "check", {"code": "private-invalid-value"}),
                    ToolCall("good", "check", {"code": "OK-12"}),
                    Finish("tool_calls"),
                ],
                [Finish()],
            ]
        )
        events = InMemoryEventSink()
        result = await Agent(
            AgentComponents(
                provider,
                tools=(FunctionTool(definition, execute),),
                schema_validator=JsonSchemaValidator(),
                approval_policy=policy,
            ),
            event_sink=events,
        ).run("q")
        self.assertEqual(len(policy.requests), 1)
        self.assertEqual(calls, [{"code": "OK-12"}])
        self.assertTrue(result.messages[2].is_error)
        self.assertNotIn("private-invalid-value", result.messages[2].content)
        self.assertFalse(result.messages[3].is_error)

    async def test_validator_mutation_cannot_replace_approved_or_executed_arguments(
        self,
    ):
        class Mutating(SubsetSchemaValidator):
            def validate(self, schema, arguments):
                arguments["value"]["items"].append("injected")
                schema["properties"].clear()

        calls, policy = [], StaticApprovalPolicy(True)

        async def execute(arguments, context):
            calls.append(arguments)
            return ToolOutput("ok")

        schema = {"type": "object", "properties": {"value": {"type": "object"}}}
        definition = ToolDefinition("echo", "Echo", schema, requires_approval=True)
        provider = FakeProvider(
            [
                [ToolCall("c", "echo", {"value": {"items": []}}), Finish("tool_calls")],
                [Finish()],
            ]
        )
        result = await Agent(
            AgentComponents(
                provider,
                tools=(FunctionTool(definition, execute),),
                schema_validator=Mutating(),
                approval_policy=policy,
            )
        ).run("q")
        self.assertEqual(calls, [{"value": {"items": []}}])
        self.assertEqual(policy.requests[0].call.arguments, calls[0])
        self.assertEqual(result.messages[1].tool_calls[0].arguments, calls[0])
        self.assertEqual(schema["properties"], {"value": {"type": "object"}})

    async def test_validator_is_consumed_before_approval_and_execution(self):
        trace, contexts = [], []

        class Approval:
            async def review(self, context):
                trace.append("approve")
                contexts.append(context)
                return await StaticApprovalPolicy(True).review(context)

        async def execute(arguments, context):
            trace.append("execute")
            contexts.append(context)
            return ToolOutput("ok")

        definition = ToolDefinition(
            "echo", "Echo", {"type": "object"}, requires_approval=True
        )
        provider = FakeProvider(
            [[ToolCall("call", "echo", {}), Finish("tool_calls")], [Finish()]]
        )
        async with (
            await AgentRuntimeBuilder()
            .with_provider(provider)
            .with_profile(
                AgentProfile(
                    "record", schema_validator_factory=lambda: RecordingValidator(trace)
                )
            )
            .with_tools((FunctionTool(definition, execute),))
            .with_approval_policy(Approval())
            .build() as runtime
        ):
            await runtime.run("s", "q")
        self.assertEqual(trace, ["validate", "approve", "execute"])
        self.assertEqual(
            [(c.run_id, c.turn_id) for c in contexts],
            [
                (
                    provider.request_contexts[0].run_id,
                    provider.request_contexts[0].turn_id,
                )
            ]
            * 2,
        )

    async def test_rejected_arguments_do_not_reach_approval_or_gate(self):
        class Reject:
            def supports(self, schema):
                return True

            def validate(self, schema, arguments):
                raise ToolValidationError(("$: rejected by injected validator",))

        policy = StaticApprovalPolicy(True)
        called = []

        async def execute(arguments, context):
            called.append(context)
            return ToolOutput("bad")

        definition = ToolDefinition(
            "echo", "Echo", {"type": "object"}, requires_approval=True
        )
        events = InMemoryEventSink()
        result = await Agent(
            AgentComponents(
                FakeProvider(
                    [[ToolCall("c", "echo", {}), Finish("tool_calls")], [Finish()]]
                ),
                tools=(FunctionTool(definition, execute),),
                approval_policy=policy,
                schema_validator=Reject(),
            ),
            event_sink=events,
        ).run("q")
        self.assertEqual(policy.requests, [])
        self.assertEqual(called, [])
        self.assertTrue(result.messages[2].is_error)
        self.assertIn("tool_validation_failed", [event.kind for event in events.events])

    async def test_validator_bug_is_internal_and_preserves_cause(self):
        class Broken(SubsetSchemaValidator):
            def validate(self, schema, arguments):
                raise RuntimeError("private diagnostic")

        async def execute(arguments, context):
            self.fail("must not execute")

        events = InMemoryEventSink()
        agent = Agent(
            AgentComponents(
                FakeProvider([[ToolCall("c", "echo", {}), Finish("tool_calls")]]),
                tools=(
                    FunctionTool(
                        ToolDefinition("echo", "Echo", {"type": "object"}), execute
                    ),
                ),
                schema_validator=Broken(),
            ),
            event_sink=events,
        )
        with self.assertRaises(InternalError) as caught:
            await agent.run("q")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertNotIn("private", repr(events.events))

    async def test_builder_override_skips_profile_validator_factory(self):
        def unused():
            self.fail("overridden factory ran")

        validator = RecordingValidator([])

        async def execute(arguments, context):
            return ToolOutput("ok")

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(
                FakeProvider(
                    [
                        [ToolCall("c", "echo", {}), Finish("tool_calls")],
                        [Finish()],
                    ]
                )
            )
            .with_profile(AgentProfile("p", schema_validator_factory=unused))
            .with_schema_validator(validator)
            .with_tools(
                (
                    FunctionTool(
                        ToolDefinition("echo", "Echo", {"type": "object"}), execute
                    ),
                )
            )
            .build()
        )
        async with runtime:
            await runtime.run("s", "q")
        self.assertEqual(validator.trace, ["validate"])

    async def test_plugin_borrows_isolated_read_only_event_sink(self):
        observed, cleanups = [], []

        class Sink:
            def emit(self, event):
                observed.append(event.kind)
                raise RuntimeError("private sink error")

            async def aclose(self):
                cleanups.append("observer")

        def plugin(api: PluginAPI):
            api.event_sink.emit(
                make_agent_event("context_prepared", "capability", source="test-plugin")
            )
            with self.assertRaises(AttributeError):
                api.event_sink = Sink()
            api.register_cleanup(lambda: cleanups.append("plugin"))

        runtime = await (
            AgentRuntimeBuilder()
            .with_provider(FakeProvider([[Finish()]]))
            .with_event_sink(Sink(), ownership="owned")
            .with_plugin("observe", plugin)
            .build()
        )
        async with runtime:
            await runtime.run("s", "q")
        self.assertIn("context_prepared", observed)
        self.assertEqual(cleanups, ["plugin", "observer"])
        self.assertTrue(runtime.event_sink_failures)

    def test_tool_context_id_validation_and_legacy_defaults(self):
        definition = ToolDefinition("echo", "Echo", {"type": "object"})
        args = (ToolCall("c", "echo", {}), definition, 1, 1)
        self.assertIsNone(ToolExecutionContext(*args).run_id)
        self.assertEqual(
            ToolExecutionContext(*args, run_id="r", turn_id="t").turn_id, "t"
        )
        for values in (
            {"turn_id": "t"},
            {"run_id": " "},
            {"run_id": "r", "turn_id": " "},
        ):
            with self.assertRaises(ValueError):
                ToolExecutionContext(*args, **values)

    async def test_timeout_and_cancel_remain_distinct(self):
        entered = asyncio.Event()

        async def wait(arguments, context):
            entered.set()
            await asyncio.Event().wait()

        for timeout in (0, None):
            entered.clear()
            definition = ToolDefinition(
                "wait", "Wait", {"type": "object"}, timeout_seconds=timeout
            )
            events = InMemoryEventSink()
            agent = Agent(
                AgentComponents(
                    FakeProvider(
                        [
                            [ToolCall("c", "wait", {}), Finish("tool_calls")],
                            [Finish()],
                        ]
                    ),
                    tools=(FunctionTool(definition, wait),),
                ),
                event_sink=events,
            )
            task = asyncio.create_task(agent.run("q"))
            await entered.wait()
            if timeout is None:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(
                    any(message.role == "tool" for message in agent.messages)
                )
                self.assertIn("run_cancelled", [e.kind for e in events.events])
            else:
                result = await task
                self.assertIn("outcome may be unknown", result.messages[2].content)
                self.assertIn("do not retry automatically", result.messages[2].content)
