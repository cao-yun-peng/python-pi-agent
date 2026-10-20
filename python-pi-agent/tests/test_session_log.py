from __future__ import annotations

import unittest

from pi_agent_py import (
    DurableSessionEvent,
    InMemorySessionStore,
    Message,
    SessionAppendPlan,
    SessionCompaction,
    SessionCompactionPlan,
    SessionConflictError,
    SessionReplayError,
    SessionValidationError,
    ToolCall,
    fork_session_events,
    prepare_session_append,
    prepare_session_compaction,
    replay_session,
)


class SessionReplayTests(unittest.TestCase):
    def test_replays_ordered_message_events(self) -> None:
        events = (
            DurableSessionEvent(
                "demo",
                1,
                "message_committed",
                Message(role="user", content="hello"),
            ),
            DurableSessionEvent(
                "demo",
                2,
                "message_committed",
                Message(role="assistant", content="hi"),
            ),
        )

        snapshot = replay_session("demo", events)

        self.assertEqual(snapshot.version, 2)
        self.assertEqual(
            [(message.role, message.content) for message in snapshot.messages],
            [("user", "hello"), ("assistant", "hi")],
        )

    def test_rejects_a_sequence_gap(self) -> None:
        events = (
            DurableSessionEvent(
                "demo",
                2,
                "message_committed",
                Message(role="user", content="missing sequence one"),
            ),
        )

        with self.assertRaisesRegex(
            SessionReplayError,
            "expected event sequence 1, but received 2",
        ):
            replay_session("demo", events)

    def test_rejects_an_event_from_another_session(self) -> None:
        events = (
            DurableSessionEvent(
                "other",
                1,
                "message_committed",
                Message(role="user", content="wrong session"),
            ),
        )

        with self.assertRaisesRegex(SessionReplayError, "not 'demo'"):
            replay_session("demo", events)


class SessionAppendTransitionTests(unittest.TestCase):
    def test_calculates_append_without_mutating_inputs(self) -> None:
        current_events = [
            DurableSessionEvent(
                "demo",
                1,
                "message_committed",
                Message(role="user", content="hello"),
            )
        ]
        new_messages = [Message(role="assistant", content="hi")]

        plan = prepare_session_append(
            "demo",
            current_events,
            expected_version=1,
            messages=new_messages,
        )

        self.assertIsInstance(plan, SessionAppendPlan)
        self.assertEqual(len(current_events), 1)
        self.assertEqual(len(new_messages), 1)
        self.assertEqual([event.sequence for event in plan.new_events], [2])
        self.assertEqual(plan.snapshot.version, 2)
        self.assertEqual(
            [message.content for message in plan.snapshot.messages],
            ["hello", "hi"],
        )

    def test_rejects_stale_version_before_creating_events(self) -> None:
        current_events = (
            DurableSessionEvent(
                "demo",
                1,
                "message_committed",
                Message(role="user", content="hello"),
            ),
        )

        with self.assertRaises(SessionConflictError):
            prepare_session_append(
                "demo",
                current_events,
                expected_version=0,
                messages=[Message(role="assistant", content="stale")],
            )


class SessionCompactionTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        messages = (
            Message(role="system", content="Be helpful."),
            Message(role="user", content="old question"),
            Message(role="assistant", content="old answer"),
            Message(role="user", content="recent question"),
            Message(role="assistant", content="recent answer"),
        )
        self.events = tuple(
            DurableSessionEvent(
                "demo",
                sequence,
                "message_committed",
                message,
            )
            for sequence, message in enumerate(messages, start=1)
        )
        self.compaction = SessionCompaction(
            replace_start=1,
            replaced_message_count=2,
            summary_message=Message(role="user", content="summary of old turn"),
        )

    def test_appends_one_event_and_replays_the_compacted_projection(self) -> None:
        plan = prepare_session_compaction(
            "demo",
            self.events,
            expected_version=5,
            compaction=self.compaction,
        )

        self.assertIsInstance(plan, SessionCompactionPlan)
        self.assertEqual(plan.event.sequence, 6)
        self.assertEqual(plan.event.kind, "context_compacted")
        self.assertEqual(plan.snapshot.version, 6)
        self.assertEqual(
            [message.content for message in plan.snapshot.messages],
            [
                "Be helpful.",
                "summary of old turn",
                "recent question",
                "recent answer",
            ],
        )

    def test_rejects_a_stale_version(self) -> None:
        with self.assertRaises(SessionConflictError):
            prepare_session_compaction(
                "demo",
                self.events,
                expected_version=4,
                compaction=self.compaction,
            )

    def test_replay_rejects_a_compaction_outside_the_history(self) -> None:
        invalid = DurableSessionEvent(
            "demo",
            6,
            "context_compacted",
            compaction=SessionCompaction(
                replace_start=1,
                replaced_message_count=10,
                summary_message=Message(role="user", content="invalid"),
            ),
        )

        with self.assertRaisesRegex(SessionReplayError, "exceeds"):
            replay_session("demo", (*self.events, invalid))


class SessionForkTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_events = [
            DurableSessionEvent(
                "source",
                1,
                "message_committed",
                Message(role="user", content="first"),
            ),
            DurableSessionEvent(
                "source",
                2,
                "message_committed",
                Message(role="assistant", content="second"),
            ),
            DurableSessionEvent(
                "source",
                3,
                "message_committed",
                Message(role="user", content="third"),
            ),
        ]

    def test_forks_a_prefix_without_mutating_source_events(self) -> None:
        forked_events = fork_session_events(
            self.source_events,
            "branch",
            at_version=2,
        )

        self.assertEqual(len(self.source_events), 3)
        self.assertEqual(
            [event.session_id for event in self.source_events],
            ["source", "source", "source"],
        )
        self.assertEqual(
            [event.session_id for event in forked_events],
            ["branch", "branch"],
        )
        self.assertEqual([event.sequence for event in forked_events], [1, 2])
        self.assertEqual(
            [event.message.content for event in forked_events],
            ["first", "second"],
        )
        self.assertEqual(replay_session("branch", forked_events).version, 2)

    def test_forks_the_full_history_by_default(self) -> None:
        forked_events = fork_session_events(self.source_events, "branch")

        self.assertEqual(len(forked_events), 3)
        self.assertEqual(forked_events[-1].message.content, "third")

    def test_forks_and_replays_a_compaction_event(self) -> None:
        compacted_event = DurableSessionEvent(
            "source",
            4,
            "context_compacted",
            compaction=SessionCompaction(
                replace_start=0,
                replaced_message_count=2,
                summary_message=Message(role="user", content="summary"),
            ),
        )

        forked_events = fork_session_events(
            [*self.source_events, compacted_event],
            "branch",
        )

        self.assertEqual(forked_events[-1].kind, "context_compacted")
        self.assertEqual(
            [
                message.content
                for message in replay_session("branch", forked_events).messages
            ],
            ["summary", "third"],
        )

    def test_deep_copies_mutable_message_content_between_branches(self) -> None:
        source_arguments = {"filters": {"city": "Beijing"}}
        source_events = [
            DurableSessionEvent(
                "source",
                1,
                "message_committed",
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ToolCall(
                            id="call-1",
                            name="weather",
                            arguments=source_arguments,
                        ),
                    ),
                ),
            )
        ]

        forked_events = fork_session_events(source_events, "branch")
        forked_filters = forked_events[0].message.tool_calls[0].arguments[
            "filters"
        ]
        self.assertIsInstance(forked_filters, dict)
        forked_filters["city"] = "Shanghai"

        self.assertEqual(source_arguments["filters"]["city"], "Beijing")

    def test_allows_forking_before_the_first_event(self) -> None:
        forked_events = fork_session_events(
            self.source_events,
            "branch",
            at_version=0,
        )

        self.assertEqual(forked_events, ())

    def test_rejects_a_version_past_the_source_history(self) -> None:
        with self.assertRaisesRegex(
            SessionValidationError,
            "at_version 4 exceeds source version 3",
        ):
            fork_session_events(
                self.source_events,
                "branch",
                at_version=4,
            )

    def test_rejects_a_negative_version(self) -> None:
        with self.assertRaisesRegex(
            SessionValidationError,
            "at_version must not be negative",
        ):
            fork_session_events(
                self.source_events,
                "branch",
                at_version=-1,
            )

    def test_rejects_using_the_source_id_as_the_target_id(self) -> None:
        with self.assertRaisesRegex(
            SessionValidationError,
            "must differ",
        ):
            fork_session_events(self.source_events, "source")

    def test_rejects_a_corrupt_source_log(self) -> None:
        corrupt_events = [
            DurableSessionEvent(
                "source",
                2,
                "message_committed",
                Message(role="user", content="gap"),
            )
        ]

        with self.assertRaises(SessionReplayError):
            fork_session_events(corrupt_events, "branch")


class InMemorySessionEventLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_creates_events_that_load_replays(self) -> None:
        store = InMemorySessionStore()
        messages = (
            Message(role="user", content="hello"),
            Message(role="assistant", content="hi"),
        )

        appended = await store.append("demo", 0, messages)
        events = await store.read_events("demo")
        loaded = await store.load("demo")

        self.assertEqual([event.sequence for event in events], [1, 2])
        self.assertEqual(
            [event.kind for event in events],
            ["message_committed", "message_committed"],
        )
        self.assertEqual(appended, loaded)
        self.assertEqual(loaded.messages, messages)

    async def test_rejects_append_from_a_stale_version(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [Message(role="user", content="first")],
        )

        with self.assertRaises(SessionConflictError):
            await store.append(
                "demo",
                0,
                [Message(role="user", content="stale")],
            )

    async def test_compact_persists_an_event_and_allows_later_append(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "demo",
            0,
            [
                Message(role="user", content="old"),
                Message(role="assistant", content="answer"),
                Message(role="user", content="recent"),
            ],
        )

        compacted = await store.compact(
            "demo",
            3,
            SessionCompaction(
                replace_start=0,
                replaced_message_count=2,
                summary_message=Message(role="user", content="summary"),
            ),
        )
        appended = await store.append(
            "demo",
            compacted.version,
            [Message(role="assistant", content="after compaction")],
        )

        self.assertEqual(compacted.version, 4)
        self.assertEqual(appended.version, 5)
        self.assertEqual(
            [event.kind for event in await store.read_events("demo")],
            [
                "message_committed",
                "message_committed",
                "message_committed",
                "context_compacted",
                "message_committed",
            ],
        )
        self.assertEqual(
            [message.content for message in appended.messages],
            ["summary", "recent", "after compaction"],
        )

    async def test_forks_and_then_allows_the_branches_to_diverge(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "source",
            0,
            [
                Message(role="user", content="first"),
                Message(role="assistant", content="second"),
                Message(role="user", content="third"),
            ],
        )

        branch = await store.fork("source", "branch", at_version=2)
        await store.append(
            "source",
            3,
            [Message(role="assistant", content="source-only")],
        )
        await store.append(
            "branch",
            2,
            [Message(role="assistant", content="branch-only")],
        )

        self.assertEqual(branch.version, 2)
        self.assertEqual(
            [message.content for message in (await store.load("source")).messages],
            ["first", "second", "third", "source-only"],
        )
        self.assertEqual(
            [message.content for message in (await store.load("branch")).messages],
            ["first", "second", "branch-only"],
        )

    async def test_fork_rejects_an_existing_target(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "source",
            0,
            [Message(role="user", content="source")],
        )
        await store.append(
            "target",
            0,
            [Message(role="user", content="keep me")],
        )

        with self.assertRaisesRegex(SessionConflictError, "already exists"):
            await store.fork("source", "target")

        self.assertEqual(
            (await store.load("target")).messages,
            (Message(role="user", content="keep me"),),
        )

    async def test_fork_rejects_an_empty_persisted_branch(self) -> None:
        store = InMemorySessionStore()
        await store.append(
            "source",
            0,
            [Message(role="user", content="first")],
        )

        with self.assertRaisesRegex(SessionValidationError, "at least one event"):
            await store.fork("source", "branch", at_version=0)

    async def test_fork_rejects_the_same_source_and_target_id(self) -> None:
        store = InMemorySessionStore()

        with self.assertRaisesRegex(SessionValidationError, "must differ"):
            await store.fork("same", "same")


if __name__ == "__main__":
    unittest.main()
