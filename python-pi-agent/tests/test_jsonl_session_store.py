from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pi_agent_py import (
    AgentSession,
    EventProvenance,
    FakeProvider,
    Finish,
    JsonlSessionStore,
    Message,
    SessionEventContext,
    SessionCompaction,
    SessionConflictError,
    SessionReplayError,
    SessionStorageError,
    TextDelta,
    ToolCall,
    WaitUntilCancelled,
)


class JsonlSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trips_complete_message_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            store = JsonlSessionStore(path)
            messages = (
                Message(
                    role="assistant",
                    content="",
                    thinking="先查天气",
                    tool_calls=(ToolCall("call-1", "weather", {"city": "成都"}),),
                ),
                Message(
                    role="tool",
                    content="晴，26°C",
                    tool_call_id="call-1",
                    tool_name="weather",
                    structured_content={"temperature": 26, "unit": "celsius"},
                ),
            )

            await store.append("demo", 0, messages)
            reopened = JsonlSessionStore(path)
            snapshot = await reopened.load("demo")

            self.assertEqual(snapshot.messages, messages)
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [record["schema_version"] for record in records],
                [4, 4],
            )
            self.assertEqual([record["sequence"] for record in records], [1, 2])

    async def test_round_trips_run_turn_and_provenance_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            store = JsonlSessionStore(path)
            context = SessionEventContext(
                run_id="run-1",
                turn_id="run-1:turn:1",
                provenance=EventProvenance(
                    source="user",
                    actor_id="user",
                ),
            )

            await store.append(
                "demo",
                0,
                [Message(role="user", content="hello")],
                event_context=context,
            )

            event = (await JsonlSessionStore(path).read_events("demo"))[0]
            self.assertEqual(event.event_id, "demo:1")
            self.assertEqual(event.run_id, "run-1")
            self.assertEqual(event.turn_id, "run-1:turn:1")
            self.assertEqual(event.provenance, context.provenance)

    async def test_reads_legacy_v1_message_without_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            record = {
                "schema_version": 1,
                "session_id": "legacy",
                "sequence": 1,
                "kind": "message_committed",
                "message": {
                    "role": "assistant",
                    "content": "旧消息",
                    "tool_calls": [],
                    "tool_call_id": None,
                    "tool_name": None,
                    "is_error": False,
                    "structured_content": None,
                },
            }
            path.write_text(
                json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            snapshot = await JsonlSessionStore(path).load("legacy")
            event = (await JsonlSessionStore(path).read_events("legacy"))[0]

            self.assertEqual(
                snapshot.messages,
                (Message(role="assistant", content="旧消息"),),
            )
            self.assertEqual(event.event_id, "legacy:1")
            self.assertEqual(event.provenance.source, "legacy")

    async def test_reads_legacy_v2_message_with_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            record = {
                "schema_version": 2,
                "session_id": "legacy",
                "sequence": 1,
                "kind": "message_committed",
                "message": {
                    "role": "assistant",
                    "content": "旧回答",
                    "thinking": "旧思考",
                    "tool_calls": [],
                    "tool_call_id": None,
                    "tool_name": None,
                    "is_error": False,
                    "structured_content": None,
                },
            }
            path.write_text(
                json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            snapshot = await JsonlSessionStore(path).load("legacy")

            self.assertEqual(
                snapshot.messages,
                (
                    Message(
                        role="assistant",
                        content="旧回答",
                        thinking="旧思考",
                    ),
                ),
            )

    async def test_reads_legacy_v3_event_with_compaction_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            record = {
                "schema_version": 3,
                "session_id": "legacy-v3",
                "sequence": 1,
                "kind": "message_committed",
                "message": {
                    "role": "user",
                    "content": "旧 v3 消息",
                    "thinking": "",
                    "tool_calls": [],
                    "tool_call_id": None,
                    "tool_name": None,
                    "is_error": False,
                    "structured_content": None,
                },
                "compaction": None,
            }
            path.write_text(
                json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            event = (await JsonlSessionStore(path).read_events("legacy-v3"))[0]

            self.assertEqual(event.event_id, "legacy-v3:1")
            self.assertEqual(event.provenance.source, "legacy")

    async def test_compaction_survives_reopen_and_keeps_original_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            store = JsonlSessionStore(path)
            await store.append(
                "demo",
                0,
                [
                    Message(role="user", content="old"),
                    Message(role="assistant", content="old answer"),
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
            reopened = JsonlSessionStore(path)

            self.assertEqual(compacted, await reopened.load("demo"))
            self.assertEqual(compacted.version, 4)
            self.assertEqual(
                [message.content for message in compacted.messages],
                ["summary", "recent"],
            )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-1]["kind"], "context_compacted")
            self.assertIsNone(records[-1]["message"])
            self.assertEqual(
                records[-1]["compaction"]["replaced_message_count"],
                2,
            )

    async def test_new_store_instance_resumes_agent_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            first = AgentSession(
                "learning",
                JsonlSessionStore(path),
                FakeProvider([[TextDelta("我记住了。"), Finish()]]),
            )
            await first.run("项目代号是松鼠。")

            resumed_provider = FakeProvider(
                [[TextDelta("项目代号是松鼠。"), Finish()]]
            )
            resumed = AgentSession(
                "learning",
                JsonlSessionStore(path),
                resumed_provider,
            )
            result = await resumed.run("项目代号是什么？")

            self.assertEqual(result.message.content, "项目代号是松鼠。")
            self.assertEqual(
                [message.role for message in resumed_provider.requests[0]],
                ["user", "assistant", "user"],
            )

    async def test_reopened_store_rejects_stale_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            await JsonlSessionStore(path).append(
                "demo",
                0,
                [Message(role="user", content="first")],
            )

            with self.assertRaises(SessionConflictError):
                await JsonlSessionStore(path).append(
                    "demo",
                    0,
                    [Message(role="user", content="stale")],
                )

    async def test_fork_survives_reopen_and_diverges_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            store = JsonlSessionStore(path)
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
            reopened = JsonlSessionStore(path)
            await reopened.append(
                "source",
                3,
                [Message(role="assistant", content="source-only")],
            )
            await reopened.append(
                "branch",
                2,
                [Message(role="assistant", content="branch-only")],
            )

            self.assertEqual(branch.version, 2)
            self.assertEqual(
                [
                    message.content
                    for message in (await reopened.load("source")).messages
                ],
                ["first", "second", "third", "source-only"],
            )
            self.assertEqual(
                [
                    message.content
                    for message in (await reopened.load("branch")).messages
                ],
                ["first", "second", "branch-only"],
            )

    async def test_fork_rejects_an_existing_target_without_overwriting_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            store = JsonlSessionStore(path)
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
                (await JsonlSessionStore(path).load("target")).messages,
                (Message(role="user", content="keep me"),),
            )

    async def test_rejects_malformed_log_and_maps_io_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            path.write_text('{"schema_version":1}\n', encoding="utf-8")

            with self.assertRaisesRegex(SessionReplayError, "line 1"):
                await JsonlSessionStore(path).load("demo")

            with self.assertRaises(SessionStorageError):
                await JsonlSessionStore(Path(directory)).load("demo")

    async def test_cancellation_keeps_user_message_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            provider = FakeProvider([[WaitUntilCancelled()]])
            session = AgentSession(
                "cancelled",
                JsonlSessionStore(path),
                provider,
            )

            task = asyncio.create_task(session.run("请保存这句话"))
            await provider.blocked.wait()
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task

            snapshot = await JsonlSessionStore(path).load("cancelled")
            self.assertEqual(
                snapshot.messages,
                (Message(role="user", content="请保存这句话"),),
            )

    async def test_reopens_and_recovers_a_trailing_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.jsonl"
            store = JsonlSessionStore(path)
            await store.append(
                "interrupted",
                0,
                [
                    Message(role="user", content="call the service"),
                    Message(
                        role="assistant",
                        content="",
                        tool_calls=(
                            ToolCall("call-restart", "external_write", {}),
                        ),
                    ),
                ],
            )

            provider = FakeProvider([[TextDelta("Recovery observed."), Finish()]])
            session = AgentSession(
                "interrupted",
                JsonlSessionStore(path),
                provider,
            )
            result = await session.run("continue after restart")

            recovered = result.messages[2]
            self.assertEqual(recovered.role, "tool")
            self.assertEqual(recovered.tool_call_id, "call-restart")
            self.assertTrue(recovered.is_error)
            self.assertEqual(
                recovered.structured_content,
                {
                    "status": "interrupted",
                    "outcome": "unknown",
                    "recovered": True,
                },
            )
            reopened = await JsonlSessionStore(path).load("interrupted")
            self.assertEqual(reopened.messages, result.messages)


if __name__ == "__main__":
    unittest.main()
