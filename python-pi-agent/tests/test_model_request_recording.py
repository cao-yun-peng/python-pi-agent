"""Persisted request inputs, fail-closed dispatch and independent-process restore."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pi_agent_py.api import (
    AgentComponents, AgentRuntimeBuilder, AgentSession, FileModelRequestStore, Finish,
    InMemoryModelRequestStore, InMemorySessionStore, Message, Middleware,
    ModelRequestTarget, ProviderRequestContext, ProviderState, RecordedModelRequest,
    RecordingModelProvider, SessionConflictError, SessionReplayError,
    SessionStorageError, SessionValidationError, TextDelta, ToolCall, ToolDefinition,
    tool,
)
from pi_agent_py.errors import ProviderTransportError
from pi_agent_py.providers.runtime import ResilientModelRuntime
from pi_agent_py.ports import ProviderCandidate
from pi_agent_py.storage import model_requests as storage
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled


def sample_request():
    return RecordedModelRequest(
        ProviderRequestContext("request", "attempt", "agent_turn", 1,
                               session_id="session", run_id="run", turn_id="turn"),
        ModelRequestTarget("fake", "fixture", {"temperature": 0.2}),
        (
            Message("system", "retrieved-only"),
            Message("assistant", "", tool_calls=(ToolCall("call", "echo", {"value": "x"}),),
                    thinking="reasoning", provider_state=(ProviderState("fake", {"opaque": [1]}),)),
            Message("tool", "result", tool_call_id="call", tool_name="echo",
                    structured_content={"nested": [1]}),
        ),
        (ToolDefinition("echo", "Return a value.", {"type": "object", "properties": {
            "value": {"type": "string"}}}, "read", True, True, True, 2.5),),
    )


class RequestValueTests(unittest.TestCase):
    def test_v2_preserves_tool_rules_and_v1_restores_empty_rules(self):
        original = sample_request()
        request = replace(original, tools=(replace(original.tools[0], prompt_guidelines=("Use echo.",)),))
        encoded = storage._encode(request)
        self.assertEqual(storage._decode(encoded), request)
        legacy = json.loads(encoded)
        legacy["schema_version"] = 1
        del legacy["tools"][0]["prompt_guidelines"]
        self.assertEqual(storage._decode(json.dumps(legacy).encode()), original)
        for invalid in ("characters", [42], None):
            record = json.loads(encoded)
            record["tools"][0]["prompt_guidelines"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(SessionReplayError):
                storage._decode(json.dumps(record).encode())

    def test_codec_round_trip_includes_every_provider_argument(self):
        request = sample_request()
        self.assertEqual(storage._decode(storage._encode(request)), request)
        self.assertNotIn("retrieved-only", repr(request))
        self.assertNotIn("reasoning", repr(request))

    def test_input_and_public_parameter_values_are_detached(self):
        args = {"nested": [1]}
        message = Message("assistant", "", tool_calls=(ToolCall("call", "echo", args),))
        request = replace(sample_request(), messages=(message,))
        args["nested"].append(2)
        self.assertEqual(request.messages[0].tool_calls[0].arguments, {"nested": [1]})
        parameters = {"stop": ["end"]}
        target = ModelRequestTarget("fake", "fixture", parameters)
        parameters["stop"].append("other")
        returned = target.parameters["stop"]
        returned.append("mutated")
        self.assertEqual(target.parameters["stop"], ["end"])

    def test_codec_rejects_unsupported_malformed_and_nonfinite_records(self):
        baseline = json.loads(storage._encode(sample_request()))
        cases = []
        for key, value in (("schema_version", 3), ("message_schema_version", 7),
                           ("schema_version", True), ("tools", {})):
            cases.append({**baseline, key: value})
        cases.append({**baseline, "context": {**baseline["context"], "purpose": "unknown"}})
        cases.append({**baseline, "target": {**baseline["target"], "extra": "private"}})
        cases.append({**baseline, "messages": [{**baseline["messages"][2], "is_error": "yes"}]})
        cases.append({**baseline, "tools": [{**baseline["tools"][0], "side_effects": "unknown"}]})
        cases.append({**baseline, "tools": [{**baseline["tools"][0], "timeout_seconds": True}]})
        for record in cases:
            with self.subTest(record=record), self.assertRaises(SessionReplayError):
                storage._decode(json.dumps(record).encode())
        for encoded in (b'{"schema_version":1,"schema_version":1}', b"NaN", b"1e999", b"\xff", b"{"):
            with self.subTest(encoded=encoded), self.assertRaises(SessionReplayError):
                storage._decode(encoded)


class RequestRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_builder_uses_recording_provider_through_the_public_facade(self):
        saved, raw = InMemoryModelRequestStore(), FakeProvider([[TextDelta("done"), Finish()]])
        target = sample_request().target
        provider = RecordingModelProvider(raw, saved, target)
        runtime = await AgentRuntimeBuilder().with_provider(
            provider, name=target.provider, model=target.model,
        ).build()
        async with runtime, await runtime.open_session("session") as session:
            result = await session.run("question")
        record, = await saved.read_requests("session")
        self.assertEqual(result.message.content, "done")
        self.assertEqual(record.messages, raw.requests[0])
        self.assertEqual(record.target, target)

    async def test_store_commit_precedes_dispatch_and_copies_are_isolated(self):
        saved = InMemoryModelRequestStore()
        request = sample_request()

        class MutatingProvider:
            async def stream(self, messages, tools=(), *, context):
                recorded = await saved.read_requests("session")
                self_test.assertEqual(recorded, (request,))
                messages[2].structured_content["nested"].append(9)
                yield Finish()

        self_test = self
        recorder = RecordingModelProvider(MutatingProvider(), saved, request.target)
        self.assertEqual([e async for e in recorder.stream(
            request.messages, request.tools, context=request.context)], [Finish()])
        records = await saved.read_requests()
        self.assertEqual(records, (request,))
        records[0].messages[2].structured_content["nested"].append(10)
        self.assertEqual(await saved.read_requests(), (request,))
        self.assertEqual(await saved.read_requests("different"), ())
        with self.assertRaises(SessionConflictError):
            await saved.record(request)

    async def test_session_saves_rag_and_tool_continuation_without_changing_history(self):
        @tool
        async def echo(value: str) -> str:
            """Return the supplied value."""
            return value

        class Rag:
            async def prepare(self, context, plan):
                return replace(plan, messages=(Message("system", "retrieved-only"),) + plan.messages)

        saved, session_store = InMemoryModelRequestStore(), InMemorySessionStore()
        raw = FakeProvider([[ToolCall("call", "echo", {"value": "x"}), Finish("tool_calls")],
                            [TextDelta("done"), Finish()]])
        provider = RecordingModelProvider(raw, saved, sample_request().target)
        async with AgentSession("session", session_store, AgentComponents(
            provider, tools=(echo,), middleware=(Middleware("rag", provider_request=Rag()),),
        )) as session:
            result = await session.run("question")
        records = await saved.read_requests("session")
        self.assertEqual(len(records), 2)
        for index, record in enumerate(records):
            self.assertEqual(record.messages, raw.requests[index])
            self.assertEqual(record.tools, raw.requested_tools[index])
            self.assertEqual(record.context, raw.request_contexts[index])
            self.assertEqual(record.messages[0].content, "retrieved-only")
        self.assertNotIn("retrieved-only", repr(result.messages))
        self.assertNotIn("retrieved-only", repr(await session_store.read_events("session")))
        self.assertEqual(records[1].messages[-1].role, "tool")

    async def test_storage_failure_prevents_any_provider_execution(self):
        class BrokenStore(InMemoryModelRequestStore):
            async def record(self, request):
                raise OSError("private diagnostic")

        raw = FakeProvider([[Finish()]])
        provider = RecordingModelProvider(raw, BrokenStore(), sample_request().target)
        with self.assertRaises(SessionStorageError) as caught:
            _ = [e async for e in provider.stream((), context=sample_request().context)]
        self.assertEqual(raw.requests, [])
        self.assertNotIn("private diagnostic", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OSError)

    async def test_cancelled_persistence_prevents_dispatch(self):
        started = asyncio.Event()

        class WaitingStore(InMemoryModelRequestStore):
            async def record(self, request):
                started.set()
                await asyncio.Event().wait()

        raw = FakeProvider([[Finish()]])
        provider = RecordingModelProvider(raw, WaitingStore(), sample_request().target)

        async def consume():
            return [e async for e in provider.stream((), context=sample_request().context)]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(raw.requests, [])

    async def test_provider_failure_and_cancellation_keep_prepared_request(self):
        for steps in ([ProviderTransportError("offline")], [WaitUntilCancelled()]):
            with self.subTest(steps=steps):
                saved, raw = InMemoryModelRequestStore(), FakeProvider([steps])
                provider = RecordingModelProvider(raw, saved, sample_request().target)

                async def consume():
                    return [e async for e in provider.stream((), context=sample_request().context)]

                task = asyncio.create_task(consume())
                if isinstance(steps[0], WaitUntilCancelled):
                    await asyncio.wait_for(raw.blocked.wait(), 5)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(ProviderTransportError):
                        await task
                self.assertEqual(len(await saved.read_requests()), 1)

    async def test_retry_candidates_keep_distinct_attempt_inputs(self):
        raw = FakeProvider([[ProviderTransportError("offline")], [Finish()]])
        saved = InMemoryModelRequestStore()
        recorded = RecordingModelProvider(raw, saved, sample_request().target)
        runtime = ResilientModelRuntime(
            [ProviderCandidate("fake", recorded)], sleep=lambda _: asyncio.sleep(0),
        )
        _ = [e async for e in runtime.stream((), context=sample_request().context)]
        records = await saved.read_requests()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].context.request_id, records[1].context.request_id)
        self.assertNotEqual(records[0].context.attempt_id, records[1].context.attempt_id)

    async def test_standalone_compaction_identity_and_parent_are_preserved(self):
        saved, raw = InMemoryModelRequestStore(), FakeProvider([[Finish()]])
        context = replace(
            sample_request().context, purpose="compaction", session_id=None,
            run_id=None, turn_id=None, parent_request_id="parent-request",
        )
        provider = RecordingModelProvider(raw, saved, sample_request().target)
        _ = [e async for e in provider.stream((Message("user", "summarize"),), context=context)]
        recorded, = await saved.read_requests()
        self.assertEqual(recorded.context, context)
        self.assertEqual(recorded.messages, raw.requests[0])


class FileRequestStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_reopen_roundtrip_filters_and_conflicting_publishers(self):
        with tempfile.TemporaryDirectory() as root:
            a, b = FileModelRequestStore(root), FileModelRequestStore(root)
            request = sample_request()
            outcomes = await asyncio.gather(a.record(request), b.record(request), return_exceptions=True)
            self.assertEqual(sum(isinstance(x, SessionConflictError) for x in outcomes), 1)
            self.assertEqual(sum(x is None for x in outcomes), 1)
            self.assertEqual(await FileModelRequestStore(root).read_requests("session"), (request,))
            self.assertEqual(await b.read_requests("other"), ())
            self.assertEqual(list(Path(root).glob(".pending-*")), [])

    async def test_sync_failure_leaves_no_final_record_and_prevents_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            raw = FakeProvider([[Finish()]])
            provider = RecordingModelProvider(raw, FileModelRequestStore(root), sample_request().target)
            with patch.object(storage.os, "fsync", side_effect=OSError("private")):
                with self.assertRaises(SessionStorageError):
                    _ = [e async for e in provider.stream((), context=sample_request().context)]
            self.assertEqual(raw.requests, [])
            self.assertEqual(list(Path(root).iterdir()), [])

    async def test_cancelled_file_worker_is_joined_before_returning(self):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        real_publish = storage._publish

        def blocking_publish(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("test publisher was not released")
            real_publish(*args)

        with tempfile.TemporaryDirectory() as root:
            raw = FakeProvider([[Finish()]])
            provider = RecordingModelProvider(raw, FileModelRequestStore(root), sample_request().target)

            async def consume():
                return [e async for e in provider.stream((), context=sample_request().context)]

            with patch.object(storage, "_publish", blocking_publish):
                task = asyncio.create_task(consume())
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    task.cancel()
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            self.assertEqual(raw.requests, [])
            self.assertEqual(len(await FileModelRequestStore(root).read_requests()), 1)
            self.assertEqual(list(Path(root).glob(".pending-*")), [])

    async def test_encoding_and_read_budgets_reject_oversized_record(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SessionValidationError):
                await FileModelRequestStore(root, max_record_bytes=10).record(sample_request())
            await FileModelRequestStore(root).record(sample_request())
            with self.assertRaises(SessionReplayError):
                await FileModelRequestStore(root, max_record_bytes=10).read_requests()

    async def test_missing_root_is_empty_but_file_root_is_misconfiguration(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "records")
            self.assertEqual(await FileModelRequestStore(path).read_requests(), ())
            path.write_bytes(b"existing-file")
            with self.assertRaises(SessionStorageError):
                await FileModelRequestStore(path).read_requests()

    async def test_finalized_corruption_is_rejected_and_pending_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".pending-crash").write_bytes(b"incomplete")
            self.assertEqual(await FileModelRequestStore(root).read_requests(), ())
            await FileModelRequestStore(root).record(sample_request())
            path = next(Path(root).glob("*.request.json"))
            path.write_bytes(b"{")
            with self.assertRaises(SessionReplayError):
                await FileModelRequestStore(root).read_requests()
            path.write_bytes(storage._encode(sample_request()))
            path.rename(Path(root, "wrong.request.json"))
            with self.assertRaises(SessionReplayError):
                await FileModelRequestStore(root).read_requests()


class RequestProcessTests(unittest.TestCase):
    def test_record_process_exits_then_offline_process_restores_original_retrieval(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "44_request_recording.py"
        env = {**os.environ, "PYTHONPATH": str(example.parents[1] / "src")}
        with tempfile.TemporaryDirectory() as root:
            reference = Path(root, "reference.txt")
            reference.write_text("original retrieved fact", encoding="utf-8")
            recorded = subprocess.run(
                [sys.executable, "-B", str(example), "record", root],
                env=env, capture_output=True, text=True, timeout=20, check=True,
            )
            self.assertEqual(json.loads(recorded.stdout), {"recorded_requests": 2})
            reference.unlink()
            restored = subprocess.run(
                [sys.executable, "-B", str(example), "restore", root],
                env=env, capture_output=True, text=True, timeout=20, check=True,
            )
            output = json.loads(restored.stdout)
            self.assertEqual(output["retrieved"], ["original retrieved fact"] * 2)
            self.assertEqual(output["tool_names"], [["echo"], ["echo"]])
            self.assertEqual(output["request_numbers"], [1, 2])
            self.assertTrue(output["history_excludes_retrieval"])


if __name__ == "__main__":
    unittest.main()
