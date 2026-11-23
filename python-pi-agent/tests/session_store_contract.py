"""Shared observable six-method contract for Memory, JSONL and PostgreSQL."""

from pi_agent_py.domain import (
    ControlCancellation,
    ControlClaim,
    ControlDelivery,
    EventProvenance,
    Message,
    ProviderState,
    QueuedControl,
    SessionCompaction,
    SessionEventContext,
    ToolCall,
)
from pi_agent_py.errors import SessionConflictError, SessionValidationError


class SessionStoreContract:
    # Concrete TestCases provide async make_store() and unittest assertions.
    async def test_contract_unknown_session_and_empty_fork(self):
        store = await self.make_store()
        self.assertEqual((await store.load("missing")).version, 0)
        self.assertEqual(await store.read_events("missing"), ())
        with self.assertRaises(SessionValidationError):
            await store.fork("missing", "target")

    async def test_contract_full_messages_and_event_identity(self):
        store = await self.make_store()
        messages = (
            Message(
                "assistant",
                "",
                thinking="计划",
                tool_calls=(ToolCall("c", "read", {"p": "文档"}),),
                provider_state=(ProviderState("fake", {"opaque": [1, True]}),),
            ),
            Message(
                "tool",
                "ok",
                tool_call_id="c",
                tool_name="read",
                structured_content={"nested": [2]},
                is_error=True,
            ),
        )
        context = SessionEventContext("r", "r:turn:1", EventProvenance("model", "fake"))
        snapshot = await store.append("s", 0, messages, event_context=context)
        self.assertEqual(snapshot.messages, messages)
        self.assertEqual(snapshot, await store.load("s"))
        events = await store.read_events("s")
        self.assertEqual([e.sequence for e in events], [1, 2])
        self.assertEqual([e.event_id for e in events], ["s:1", "s:2"])
        self.assertTrue(
            all(e.run_id == "r" and e.provenance == context.provenance for e in events)
        )

    async def test_contract_stale_writes_do_not_change_history(self):
        store = await self.make_store()
        original = await store.append("s", 0, (Message("user", "hi"),))
        with self.assertRaises(SessionConflictError):
            await store.append("s", 0, (Message("assistant", "lost"),))
        with self.assertRaises(SessionConflictError):
            await store.compact(
                "s", 0, SessionCompaction(0, 1, Message("user", "summary"))
            )
        with self.assertRaises(SessionConflictError):
            await store.record_control(
                "s", 0, "control_enqueued", QueuedControl("q", "follow_up", "next", "r")
            )
        self.assertEqual(await store.load("s"), original)

    async def test_contract_controls_and_compaction_replay(self):
        store = await self.make_store()
        await store.record_control(
            "s", 0, "control_enqueued", QueuedControl("q", "follow_up", "next", "r")
        )
        await store.record_control(
            "s", 1, "control_claimed", ControlClaim("q", "c", "w", 10)
        )
        delivered = await store.record_control(
            "s",
            2,
            "control_delivered",
            ControlDelivery("q", "c", Message("user", "next")),
        )
        self.assertEqual(delivered.pending_controls, ())
        compacted = await store.compact(
            "s", 3, SessionCompaction(0, 1, Message("user", "summary"))
        )
        self.assertEqual(compacted.version, 4)
        self.assertEqual(compacted.messages, (Message("user", "summary"),))
        self.assertEqual(len(await store.read_events("s")), 4)

    async def test_contract_fork_preserves_control_ids_and_isolates_history(self):
        store = await self.make_store()
        await store.append("s", 0, (Message("user", "hi"),))
        await store.record_control(
            "s", 1, "control_enqueued", QueuedControl("q", "follow_up", "next", "r")
        )
        forked = await store.fork("s", "branch")
        self.assertEqual(forked.pending_controls[0].control.control_id, "q")
        copied = await store.read_events("branch")
        self.assertEqual(copied[0].provenance.copied_from_event_id, "s:1")
        await store.record_control(
            "branch", 2, "control_cancelled", ControlCancellation("q", "branch_only")
        )
        self.assertEqual(len((await store.load("s")).pending_controls), 1)
        with self.assertRaises(SessionConflictError):
            await store.fork("s", "branch")
        with self.assertRaises(SessionValidationError):
            await store.fork("s", "s")
        with self.assertRaises(SessionValidationError):
            await store.fork("s", "invalid", 99)
        with self.assertRaises(SessionValidationError):
            await store.fork("s", "zero", 0)
        prefix = await store.fork("s", "prefix", 1)
        self.assertEqual(prefix.version, 1)

    async def test_contract_invalid_input_leaves_no_events(self):
        store = await self.make_store()
        with self.assertRaises(SessionValidationError):
            await store.append("s", 0, ())
        with self.assertRaises(SessionValidationError):
            await store.append("s", -1, (Message("user", "hi"),))
        with self.assertRaises(SessionValidationError):
            await store.append(" ", 0, (Message("user", "hi"),))
        self.assertEqual(await store.read_events("s"), ())
