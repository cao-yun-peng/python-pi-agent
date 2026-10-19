from __future__ import annotations

import unittest

from pi_agent_py import (
    DurableSessionEvent,
    InMemorySessionStore,
    Message,
    SessionConflictError,
    SessionReplayError,
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


if __name__ == "__main__":
    unittest.main()
