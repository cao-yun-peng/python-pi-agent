"""Offline contracts for workspace paths, bounded text pages and sandbox dispatch."""

import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from pi_agent_py.api import (
    Agent, AgentComponents, AgentRuntimeBuilder, DeclaredSideEffectGate, Finish, InMemoryEventSink,
    StaticApprovalPolicy, ToolCall,
    WorkspaceReadLimits, WorkspaceReadRequest, WorkspaceFileError,
    ReadTool, SandboxedWorkspaceFiles, read_only_profile, coding_profile,
)
from pi_agent_py.domain import SandboxProcessResult
from pi_agent_py.errors import SandboxTimeoutError
from pi_agent_py.domain import SandboxProcessRequest
from pi_agent_py.tools.sandbox import DockerProcessSandbox
from pi_agent_py.testing import FakeProvider, InMemoryWorkspaceFiles


def wire(data):
    return json.dumps({"data": base64.b64encode(data).decode("ascii")})


class Sandbox:
    def __init__(self, stdout=None, *, error=None, enforcement="full", truncated=False):
        self.stdout = wire(b"one\ntwo\n") if stdout is None else stdout
        self.error, self.enforcement, self.truncated = error, enforcement, truncated
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return SandboxProcessResult(0, self.stdout, "", self.enforcement, self.truncated)


class PathTests(unittest.TestCase):
    def test_rejects_escape_absolute_windows_and_nonfile_paths(self):
        for path in ("", " ", ".", "..", "a/../b", "/etc/passwd", "C:/secret", "a\\b", "//server/x", "~/x", "x\x00y", "a" * 4097):
            with self.subTest(path=path), self.assertRaises(ValueError):
                WorkspaceReadRequest(path)
        self.assertEqual(WorkspaceReadRequest("./a//b.txt").path, "a/b.txt")

    def test_positive_integer_requests_and_budgets(self):
        for value in (0, -1, True, 1.5):
            for field in ("offset", "limit"):
                with self.assertRaises(ValueError):
                    WorkspaceReadRequest("note", **{field: value})
            for field in ("max_file_bytes", "max_output_bytes", "max_lines"):
                with self.assertRaises(ValueError):
                    WorkspaceReadLimits(**{field: value})


class MemoryReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_crlf_bom_and_version_cover_complete_raw_bytes(self):
        raw = b"\xef\xbb\xbfalpha\r\nbeta\r\ngamma"
        files = InMemoryWorkspaceFiles({"note": raw})
        first = await files.read_text(WorkspaceReadRequest("note", limit=2))
        second = await files.read_text(WorkspaceReadRequest("note", offset=first.next_offset))
        self.assertEqual(first.text, "alpha\r\nbeta\r\n")
        self.assertEqual((first.returned_lines, first.total_lines, first.next_offset), (2, 3, 3))
        self.assertEqual(second.text, "gamma")
        self.assertIsNone(second.next_offset)
        self.assertEqual(first.version, second.version)
        self.assertEqual(first.total_bytes, len(raw))
        changed = await InMemoryWorkspaceFiles({"note": raw + b"!"}).read_text(WorkspaceReadRequest("note", limit=2))
        self.assertEqual(first.text, changed.text)
        self.assertNotEqual(first.version, changed.version)

    async def test_empty_eof_and_invalid_offset(self):
        files = InMemoryWorkspaceFiles({"empty": b"", "note": b"a\n"})
        self.assertEqual((await files.read_text(WorkspaceReadRequest("empty"))).total_lines, 0)
        self.assertEqual((await files.read_text(WorkspaceReadRequest("note", 2))).text, "")
        with self.assertRaises(WorkspaceFileError) as caught:
            await files.read_text(WorkspaceReadRequest("note", 3))
        self.assertEqual(caught.exception.code, "invalid_offset")

    async def test_utf8_byte_and_line_budgets_never_skip_a_line(self):
        files = InMemoryWorkspaceFiles({"note": "你好\na\nb\n".encode()},
            limits=WorkspaceReadLimits(max_output_bytes=7, max_lines=1))
        first = await files.read_text(WorkspaceReadRequest("note", limit=9999))
        self.assertEqual(first.text, "你好\n")
        self.assertEqual(first.next_offset, 2)
        self.assertEqual((await files.read_text(WorkspaceReadRequest("note", 2))).text, "a\n")
        files = InMemoryWorkspaceFiles({"note": b"a\nb\n"}, limits=WorkspaceReadLimits(max_output_bytes=3))
        self.assertEqual((await files.read_text(WorkspaceReadRequest("note"))).text, "a\n")

    async def test_safe_failures_and_immutable_initial_catalog(self):
        original = {"note": b"ok"}
        files = InMemoryWorkspaceFiles(original)
        original["note"] = b"changed"
        self.assertEqual((await files.read_text(WorkspaceReadRequest("note"))).text, "ok")
        for content, limits, code in (
            (b"abc", WorkspaceReadLimits(max_file_bytes=2), "too_large"),
            (b"\xff", WorkspaceReadLimits(), "not_utf8"),
            (b"a\x00b", WorkspaceReadLimits(), "not_utf8"),
            (b"abcd", WorkspaceReadLimits(max_output_bytes=3), "line_too_long"),
        ):
            with self.subTest(code=code), self.assertRaises(WorkspaceFileError) as caught:
                await InMemoryWorkspaceFiles({"note": content}, limits=limits).read_text(WorkspaceReadRequest("note"))
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(WorkspaceFileError) as caught:
            await files.read_text(WorkspaceReadRequest("missing"))
        self.assertEqual(caught.exception.code, "not_found")


class SandboxReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_argv_read_only_root_and_quoted_path(self):
        sandbox = Sandbox()
        files = SandboxedWorkspaceFiles(sandbox)
        result = await files.read_text(WorkspaceReadRequest("a 'quoted' $(name).txt"))
        request = sandbox.requests[0]
        self.assertEqual(request.mode, "read-only")
        self.assertEqual(request.workdir, "/workspace")
        self.assertEqual(request.argv[:3], ("python3", "-I", "-c"))
        self.assertEqual(request.argv[4:6], ("/workspace", "a 'quoted' $(name).txt"))
        self.assertEqual(result.text, "one\ntwo\n")

    async def test_fail_closed_for_partial_truncated_malformed_and_oversized_results(self):
        for sandbox, code in (
            (Sandbox(enforcement="partial"), "unavailable"),
            (Sandbox(truncated=True), "protocol_error"),
            (Sandbox("not json private-data"), "protocol_error"),
            (Sandbox('{"data":"!!!"}'), "protocol_error"),
            (Sandbox('{"error":"secret-path"}'), "protocol_error"),
            (Sandbox(wire(b"abc")), "too_large"),
        ):
            with self.subTest(code=code), self.assertRaises(WorkspaceFileError) as caught:
                await SandboxedWorkspaceFiles(sandbox, limits=WorkspaceReadLimits(max_file_bytes=2)).read_text(WorkspaceReadRequest("note"))
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("private", str(caught.exception))

    async def test_timeout_and_cancel_propagate_to_sandbox(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class Blocking(Sandbox):
            async def run(self, request):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        files = SandboxedWorkspaceFiles(Blocking())
        task = asyncio.create_task(files.read_text(WorkspaceReadRequest("note")))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        with self.assertRaises(SandboxTimeoutError):
            await SandboxedWorkspaceFiles(Sandbox(error=SandboxTimeoutError("private"))).read_text(WorkspaceReadRequest("note"))


class ReadToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_explicitly_allow_registered_read_tool(self):
        for profile in (read_only_profile(), coding_profile()):
            provider = FakeProvider([[ToolCall("c", "read", {"path": "note"}), Finish("tool_calls")], [Finish()]])
            async with await AgentRuntimeBuilder().with_provider(provider).with_profile(profile).with_tools(
                (ReadTool(InMemoryWorkspaceFiles({"note": b"ok"})),)
            ).build() as runtime:
                result = await runtime.run("s", "read")
            output, = (m for m in result.messages if m.role == "tool")
            self.assertEqual(output.content, "ok")

    async def test_agent_cancellation_reaches_read_backend_without_completed_result(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class Blocking:
            async def read_text(self, request):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        agent = Agent(AgentComponents(FakeProvider([
            [ToolCall("c", "read", {"path": "note"}), Finish("tool_calls")],
        ]), tools=(ReadTool(Blocking()),),
            execution_gate=DeclaredSideEffectGate(allowed_side_effects=("read",))))
        task = asyncio.create_task(agent.run("read"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(m.role == "tool" for m in agent.messages))

    async def test_pipeline_validation_gate_approval_results_and_continuation(self):
        files = InMemoryWorkspaceFiles({"note": b"a\nb\n"})
        policy, sink = StaticApprovalPolicy(True), InMemoryEventSink()
        provider = FakeProvider([
            [ToolCall("bad", "read", {"path": "../secret"}),
             ToolCall("zero", "read", {"path": "note", "offset": 0}),
             ToolCall("ok", "read", {"path": "note", "limit": 1}),
             ToolCall("missing", "read", {"path": "missing"}),
             ToolCall("shape", "read", {"path": "note", "limit": "bad"}), Finish("tool_calls")], [Finish()],
        ])
        result = await Agent(AgentComponents(provider, tools=(ReadTool(files, requires_approval=True),),
            approval_policy=policy, execution_gate=DeclaredSideEffectGate(allowed_side_effects=("read",), allowed_tools=("read",))), event_sink=sink).run("read")
        outputs = [m for m in result.messages if m.role == "tool"]
        self.assertEqual([m.is_error for m in outputs], [True, True, False, True, True])
        self.assertEqual(outputs[2].structured_content["next_offset"], 2)
        self.assertIn("a\n", provider.requests[1][4].content)
        self.assertEqual(len(policy.requests), 4)
        self.assertEqual([m for m in provider.requests[1] if m.role == "tool"], outputs)
        self.assertIn("tool_execution_finished", [e.kind for e in sink.events])

    async def test_default_gate_and_denied_approval_do_not_access_backend(self):
        calls = []
        class Never:
            async def read_text(self, request):
                calls.append(request)
                raise AssertionError("backend must not be reached")
        for approve in (False, True):
            result = await Agent(AgentComponents(FakeProvider([
                [ToolCall("c", "read", {"path": "note"}), Finish("tool_calls")], [Finish()],
            ]), tools=(ReadTool(Never(), requires_approval=True),), approval_policy=StaticApprovalPolicy(approve))).run("read")
            self.assertTrue(result.messages[2].is_error)
            self.assertEqual(calls, [])


@unittest.skipUnless(os.name == "posix", "real secure helper requires POSIX dir_fd/O_NOFOLLOW")
class SecureHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_helper_reads_and_rejects_symlink_directory_and_fifo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note").write_bytes(b"real\n")
            (root / "sub").mkdir()
            (root / "link").symlink_to(root / "note")
            (root / "alias").symlink_to(root / "sub", target_is_directory=True)
            os.mkfifo(root / "fifo")
            class Helper:
                async def run(self, request):
                    argv = list(request.argv)
                    argv[0], argv[4] = sys.executable, str(root)
                    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE)
                    data, _ = await process.communicate()
                    return SandboxProcessResult(process.returncode, data.decode(), "", "full")
            files = SandboxedWorkspaceFiles(Helper())
            self.assertEqual((await files.read_text(WorkspaceReadRequest("note"))).text, "real\n")
            for path, code in (("link", "symlink_denied"), ("alias/nope", "symlink_denied"), ("sub", "not_file"), ("fifo", "not_file"), ("absent", "not_found")):
                with self.subTest(path=path), self.assertRaises(WorkspaceFileError) as caught:
                    await files.read_text(WorkspaceReadRequest(path))
                self.assertEqual(caught.exception.code, code)


@unittest.skipUnless(os.environ.get("PI_AGENT_DOCKER_IMAGE"), "set PI_AGENT_DOCKER_IMAGE to a local Linux Python image")
class DockerReadIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sandbox_file_boundaries_and_read_only_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note").write_bytes(b"real\r\nsecond\n")
            unusual = "a 'quoted' $(name).txt"
            (root / unusual).write_bytes(b"literal filename")
            sandbox = DockerProcessSandbox(root, image=os.environ["PI_AGENT_DOCKER_IMAGE"], max_output_bytes=2_000_000)
            setup = await sandbox.run(SandboxProcessRequest(("python3", "-I", "-c",
                "import os; os.mkdir('/workspace/sub'); os.symlink('/etc/passwd','/workspace/link'); "
                "os.symlink('/workspace/sub','/workspace/alias'); os.mkfifo('/workspace/fifo')")))
            self.assertEqual(setup.exit_code, 0)
            files = SandboxedWorkspaceFiles(sandbox)
            first = await files.read_text(WorkspaceReadRequest("note", limit=1))
            self.assertEqual(first.text, "real\r\n")
            self.assertEqual(first.next_offset, 2)
            self.assertEqual((await files.read_text(WorkspaceReadRequest(unusual))).text, "literal filename")
            with self.assertRaises(WorkspaceFileError) as caught:
                await SandboxedWorkspaceFiles(sandbox, limits=WorkspaceReadLimits(max_file_bytes=2)).read_text(WorkspaceReadRequest("note"))
            self.assertEqual(caught.exception.code, "too_large")
            for path, code in (("link", "symlink_denied"), ("alias/missing", "symlink_denied"), ("sub", "not_file"), ("fifo", "not_file"), ("missing", "not_found")):
                with self.subTest(path=path), self.assertRaises(WorkspaceFileError) as caught:
                    await files.read_text(WorkspaceReadRequest(path))
                self.assertEqual(caught.exception.code, code)
            denied = await sandbox.run(SandboxProcessRequest(("python3", "-I", "-c",
                "open('/workspace/no-write','w').write('x')"), mode="read-only"))
            self.assertNotEqual(denied.exit_code, 0)
            self.assertFalse((root / "no-write").exists())
