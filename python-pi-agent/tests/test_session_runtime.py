from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from pi_agent_py import (
    AgentSession,
    CompactionError,
    ConfigurationError,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    InMemorySessionStore,
    JSONObject,
    Message,
    ProviderError,
    SessionValidationError,
    TextDelta,
    TokenThresholdCompactionPolicy,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    InProcessSandbox,
    WaitUntilCancelled,
)


class MessageCountEstimator:
    def estimate(self, messages: Sequence[Message]) -> int:
        return len(messages)


def _large_result_definition() -> ToolDefinition:
    return ToolDefinition(
        name="large_result",
        description="Return a large model-visible result.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


class AgentSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_auto_policy_requires_a_compaction_strategy(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "requires"):
            AgentSession(
                "demo",
                InMemorySessionStore(),
                FakeProvider([]),
                compaction_policy=TokenThresholdCompactionPolicy(10),
            )

    async def test_loads_history_and_persists_each_run(self) -> None:
        provider = FakeProvider(
            [
                [TextDelta("first answer"), Finish(reason="stop")],
                [TextDelta("second answer"), Finish(reason="stop")],
            ]
        )
        store = InMemorySessionStore()
        session = AgentSession(
            "demo",
            store,
            provider,
            system_prompt="You are helpful.",
        )

        first = await session.run("first question")
        second = await session.run("second question")

        self.assertEqual(
            [message.role for message in first.messages],
            ["system", "user", "assistant"],
        )
        self.assertEqual(
            [message.role for message in second.messages],
            ["system", "user", "assistant", "user", "assistant"],
        )
        self.assertEqual(provider.requests[1], second.messages[:-1])

    async def test_provider_failure_keeps_committed_user_message(self) -> None:
        store = InMemorySessionStore()
        session = AgentSession(
            "demo",
            store,
            FakeProvider([[ProviderError("provider failed")]]),
        )

        with self.assertRaises(ProviderError):
            await session.run("remember this")

        snapshot = await store.load("demo")
        self.assertEqual(
            [(message.role, message.content) for message in snapshot.messages],
            [("user", "remember this")],
        )

    async def test_cancellation_keeps_committed_user_message(self) -> None:
        provider = FakeProvider([[WaitUntilCancelled()]])
        store = InMemorySessionStore()
        session = AgentSession("demo", store, provider)

        task = asyncio.create_task(session.run("remember this"))
        await provider.blocked.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        snapshot = await store.load("demo")
        self.assertEqual(
            [(message.role, message.content) for message in snapshot.messages],
            [("user", "remember this")],
        )

    async def test_recovers_only_missing_tool_results_before_next_prompt(
        self,
    ) -> None:
        second_call_started = asyncio.Event()
        call_count = 0

        async def sometimes_blocks(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            self.assertEqual(context.session_id, "demo")
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ToolOutput(content=f"completed {arguments['value']}")
            second_call_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        definition = ToolDefinition(
            name="step",
            description="Complete the first call and block the second.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            side_effects="external",
        )
        provider = FakeProvider(
            [
                [
                    ToolCall("call-1", "step", {"value": 1}),
                    ToolCall("call-2", "step", {"value": 2}),
                    Finish("tool_calls"),
                ],
                [TextDelta("Recovered safely."), Finish()],
            ]
        )
        store = InMemorySessionStore()
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            provider,
            tools=[FunctionTool(definition, sometimes_blocks)],
            sandbox=InProcessSandbox(),
            event_sink=events,
        )

        interrupted = asyncio.create_task(session.run("run both steps"))
        await second_call_started.wait()
        interrupted.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await interrupted

        interrupted_snapshot = await store.load("demo")
        self.assertEqual(
            [message.role for message in interrupted_snapshot.messages],
            ["user", "assistant", "tool"],
        )
        self.assertEqual(interrupted_snapshot.messages[-1].tool_call_id, "call-1")

        result = await session.run("continue without retrying")

        recovered = result.messages[3]
        self.assertEqual(
            [message.role for message in provider.requests[1]],
            ["user", "assistant", "tool", "tool", "user"],
        )
        self.assertEqual(recovered.tool_call_id, "call-2")
        self.assertTrue(recovered.is_error)
        self.assertEqual(
            recovered.structured_content,
            {
                "status": "interrupted",
                "outcome": "unknown",
                "recovered": True,
            },
        )
        self.assertIn("outcome is unknown", recovered.content)
        recovered_events = [
            event for event in events.events if event.kind == "tool_call_recovered"
        ]
        self.assertEqual(len(recovered_events), 1)
        self.assertEqual(recovered_events[0].payload["tool_call_id"], "call-2")

    async def test_rejects_non_terminal_unresolved_tool_calls(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "corrupt",
            0,
            [
                Message(role="user", content="start"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall("call-1", "write", {}),),
                ),
                Message(role="user", content="appended too early"),
            ],
        )
        provider = FakeProvider([])
        session = AgentSession("corrupt", store, provider)

        with self.assertRaisesRegex(
            SessionValidationError,
            "must remain at the session tail",
        ):
            await session.run("do not hide the damage")

        self.assertEqual(provider.requests, [])

    async def test_compacts_history_before_the_next_run(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [
                Message(role="system", content="Be helpful."),
                Message(role="user", content="old question"),
                Message(role="assistant", content="old answer"),
                Message(role="user", content="recent question"),
                Message(role="assistant", content="recent answer"),
            ],
        )
        provider = FakeProvider([[TextDelta("new answer"), Finish()]])
        strategy = FakeCompactionStrategy(["Old topic was resolved."])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            provider,
            compaction_strategy=strategy,
            event_sink=events,
        )

        compacted = await session.compact(keep_recent_turns=1)
        result = await session.run("new question")

        self.assertEqual(compacted.version, 6)
        self.assertEqual(
            [message.content for message in compacted.messages],
            [
                "Be helpful.",
                "<conversation-summary>\nOld topic was resolved.\n"
                "</conversation-summary>",
                "recent question",
                "recent answer",
            ],
        )
        self.assertEqual(strategy.requests[0][0].content, "old question")
        self.assertEqual(provider.requests[0], result.messages[:-1])
        self.assertIn(
            "compaction_completed",
            [event.kind for event in events.events],
        )

    async def test_automatically_compacts_when_threshold_is_reached(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old question"),
                Message(role="assistant", content="old answer"),
                Message(role="user", content="recent question"),
                Message(role="assistant", content="recent answer"),
            ],
        )
        provider = FakeProvider([[TextDelta("new answer"), Finish()]])
        strategy = FakeCompactionStrategy(["old topic summary"])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            provider,
            compaction_policy=TokenThresholdCompactionPolicy(
                5,
                estimator=MessageCountEstimator(),
            ),
            compaction_strategy=strategy,
            event_sink=events,
        )

        result = await session.run("new question")

        self.assertEqual(
            [message.content for message in provider.requests[0]],
            [
                "<conversation-summary>\nold topic summary\n"
                "</conversation-summary>",
                "recent question",
                "recent answer",
                "new question",
            ],
        )
        self.assertEqual(result.messages[-1].content, "new answer")
        self.assertEqual(
            [event.kind for event in await store.read_events("demo")],
            [
                "message_committed",
                "message_committed",
                "message_committed",
                "message_committed",
                "context_compacted",
                "message_committed",
                "message_committed",
            ],
        )
        self.assertEqual(
            [event.kind for event in events.events[:3]],
            [
                "compaction_evaluated",
                "compaction_started",
                "compaction_completed",
            ],
        )

    async def test_compacts_after_tool_results_before_the_next_model_request(
        self,
    ) -> None:
        async def large_result(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del arguments, context
            return ToolOutput(content="large tool result")

        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old question"),
                Message(role="assistant", content="old answer"),
                Message(role="user", content="recent question"),
                Message(role="assistant", content="recent answer"),
            ],
        )
        provider = FakeProvider(
            [
                [ToolCall("call-1", "large_result", {}), Finish("tool_calls")],
                [TextDelta("finished after compaction"), Finish()],
            ]
        )
        strategy = FakeCompactionStrategy(["old work summary"])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            provider,
            tools=[FunctionTool(_large_result_definition(), large_result)],
            compaction_policy=TokenThresholdCompactionPolicy(
                7,
                estimator=MessageCountEstimator(),
            ),
            compaction_strategy=strategy,
            event_sink=events,
        )

        result = await session.run("use the tool")

        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            [message.content for message in provider.requests[1]],
            [
                "<conversation-summary>\nold work summary\n"
                "</conversation-summary>",
                "use the tool",
                "",
                "large tool result",
            ],
        )
        self.assertEqual(
            [message.role for message in provider.requests[1]],
            ["user", "user", "assistant", "tool"],
        )
        self.assertEqual(result.message.content, "finished after compaction")
        self.assertEqual(
            [event.kind for event in await store.read_events("demo")],
            [
                "message_committed",
                "message_committed",
                "message_committed",
                "message_committed",
                "message_committed",
                "message_committed",
                "message_committed",
                "context_compacted",
                "message_committed",
            ],
        )
        self.assertEqual(
            [
                event.payload["should_compact"]
                for event in events.events
                if event.kind == "compaction_evaluated"
            ],
            [False, True],
        )

    async def test_in_loop_compaction_failure_preserves_committed_tool_result(
        self,
    ) -> None:
        async def large_result(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del arguments, context
            return ToolOutput(content="large tool result")

        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old question"),
                Message(role="assistant", content="old answer"),
                Message(role="user", content="recent question"),
                Message(role="assistant", content="recent answer"),
            ],
        )
        provider = FakeProvider(
            [[ToolCall("call-1", "large_result", {}), Finish("tool_calls")]]
        )
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            provider,
            tools=[FunctionTool(_large_result_definition(), large_result)],
            compaction_policy=TokenThresholdCompactionPolicy(
                7,
                estimator=MessageCountEstimator(),
            ),
            compaction_strategy=FakeCompactionStrategy(
                [CompactionError("summary failed")]
            ),
            event_sink=events,
        )

        with self.assertRaises(CompactionError):
            await session.run("use the tool")

        snapshot = await store.load("demo")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(
            [message.role for message in snapshot.messages[-3:]],
            ["user", "assistant", "tool"],
        )
        self.assertNotIn(
            "context_compacted",
            [event.kind for event in await store.read_events("demo")],
        )
        self.assertEqual(
            [event.kind for event in events.events[-4:]],
            [
                "compaction_started",
                "compaction_failed",
                "run_failed",
                "run_settled",
            ],
        )

    async def test_in_loop_compaction_cancellation_preserves_tool_result(
        self,
    ) -> None:
        async def large_result(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del arguments, context
            return ToolOutput(content="large tool result")

        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old question"),
                Message(role="assistant", content="old answer"),
                Message(role="user", content="recent question"),
                Message(role="assistant", content="recent answer"),
            ],
        )
        provider = FakeProvider(
            [[ToolCall("call-1", "large_result", {}), Finish("tool_calls")]]
        )
        strategy = FakeCompactionStrategy([WaitUntilCancelled()])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            provider,
            tools=[FunctionTool(_large_result_definition(), large_result)],
            compaction_policy=TokenThresholdCompactionPolicy(
                7,
                estimator=MessageCountEstimator(),
            ),
            compaction_strategy=strategy,
            event_sink=events,
        )

        task = asyncio.create_task(session.run("use the tool"))
        await strategy.blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        snapshot = await store.load("demo")
        self.assertEqual(
            [message.role for message in snapshot.messages[-3:]],
            ["user", "assistant", "tool"],
        )
        self.assertNotIn(
            "context_compacted",
            [event.kind for event in await store.read_events("demo")],
        )
        self.assertEqual(
            [event.kind for event in events.events[-3:]],
            ["compaction_cancelled", "run_cancelled", "run_settled"],
        )

    async def test_does_not_summarize_below_the_threshold(self) -> None:
        provider = FakeProvider([[TextDelta("answer"), Finish()]])
        strategy = FakeCompactionStrategy([])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            InMemorySessionStore(),
            provider,
            compaction_policy=TokenThresholdCompactionPolicy(
                10,
                estimator=MessageCountEstimator(),
            ),
            compaction_strategy=strategy,
            event_sink=events,
        )

        await session.run("question")

        self.assertEqual(strategy.requests, [])
        evaluated = events.events[0]
        self.assertEqual(evaluated.kind, "compaction_evaluated")
        self.assertFalse(evaluated.payload["should_compact"])

    async def test_auto_compaction_failure_does_not_commit_new_prompt(
        self,
    ) -> None:
        store = InMemorySessionStore()
        original = await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="recent"),
            ],
        )
        provider = FakeProvider([])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            provider,
            compaction_policy=TokenThresholdCompactionPolicy(
                4,
                estimator=MessageCountEstimator(),
            ),
            compaction_strategy=FakeCompactionStrategy(
                [CompactionError("summary failed")]
            ),
            event_sink=events,
        )

        with self.assertRaises(CompactionError):
            await session.run("new prompt")

        self.assertEqual(await store.load("demo"), original)
        self.assertEqual(provider.requests, [])
        self.assertEqual(
            [event.kind for event in events.events],
            [
                "compaction_evaluated",
                "compaction_started",
                "compaction_failed",
            ],
        )

    async def test_skips_when_threshold_reaches_without_old_turns(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [Message(role="user", content="only existing turn")],
        )
        strategy = FakeCompactionStrategy([])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            FakeProvider([[TextDelta("answer"), Finish()]]),
            compaction_policy=TokenThresholdCompactionPolicy(
                2,
                estimator=MessageCountEstimator(),
            ),
            compaction_strategy=strategy,
            event_sink=events,
        )

        await session.run("new prompt")

        self.assertEqual(strategy.requests, [])
        self.assertEqual(
            [event.kind for event in events.events[:2]],
            ["compaction_evaluated", "compaction_skipped"],
        )

    async def test_compaction_failure_does_not_append_an_event(self) -> None:
        store = InMemorySessionStore()
        original = await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="recent"),
            ],
        )
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            FakeProvider([]),
            compaction_strategy=FakeCompactionStrategy(
                [CompactionError("cannot summarize")]
            ),
            event_sink=events,
        )

        with self.assertRaises(CompactionError):
            await session.compact()

        self.assertEqual(await store.load("demo"), original)
        self.assertEqual(
            [event.kind for event in events.events],
            ["compaction_started", "compaction_failed"],
        )

    async def test_compaction_cancellation_does_not_append_an_event(self) -> None:
        store = InMemorySessionStore()
        original = await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="recent"),
            ],
        )
        strategy = FakeCompactionStrategy([WaitUntilCancelled()])
        events = InMemoryEventSink()
        session = AgentSession(
            "demo",
            store,
            FakeProvider([]),
            compaction_strategy=strategy,
            event_sink=events,
        )

        task = asyncio.create_task(session.compact())
        await strategy.blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(await store.load("demo"), original)
        self.assertEqual(
            [event.kind for event in events.events],
            ["compaction_started", "compaction_cancelled"],
        )


if __name__ == "__main__":
    unittest.main()
