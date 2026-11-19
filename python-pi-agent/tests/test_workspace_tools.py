"""Offline mutation/query contracts, plus explicitly enabled real Docker evidence."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from pi_agent_py.api import (
    Agent, AgentRuntimeBuilder, EditTool, FindTool, Finish, GrepTool,
    LsTool, ReadTool, SandboxedWorkspaceFiles, StaticApprovalPolicy, ToolCall,
    WorkspaceEditRequest, WorkspaceFileError, WorkspaceQueryLimits, WorkspaceQueryRequest,
    WorkspaceReadLimits, WorkspaceReadRequest, WorkspaceWriteRequest, WriteTool,
    coding_profile, read_only_profile,
)
from pi_agent_py.domain import SandboxProcessRequest, SandboxProcessResult
from pi_agent_py.errors import SandboxTimeoutError
from pi_agent_py.testing import FakeProvider, InMemoryWorkspaceFiles
from pi_agent_py.tools.sandbox import DockerProcessSandbox


class MemoryMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_conflict_replace_and_full_version(self):
        files = InMemoryWorkspaceFiles({}, allow_write=True)
        created = await files.write_text(WorkspaceWriteRequest("nested/new.txt", "one\n"))
        self.assertTrue(created.created)
        for expected in (None, "sha256:" + "0" * 64):
            with self.assertRaises(WorkspaceFileError) as caught:
                await files.write_text(WorkspaceWriteRequest("nested/new.txt", "bad", expected))
            self.assertEqual(caught.exception.code, "conflict")
        changed = await files.write_text(WorkspaceWriteRequest("nested/new.txt", "two\n", created.version))
        self.assertFalse(changed.created)
        self.assertNotEqual(changed.version, created.version)
        self.assertIn("+two", changed.diff)
        self.assertEqual((await files.read_text(WorkspaceReadRequest("nested/new.txt"))).version, changed.version)

    async def test_edit_exact_overlapping_ambiguity_bom_and_mixed_line_endings(self):
        raw = b"\xef\xbb\xbfa\r\nb\nc\r\n"
        files = InMemoryWorkspaceFiles({"note": raw, "ambiguous": b"aaa"}, allow_write=True)
        original = await files.read_text(WorkspaceReadRequest("note"))
        result = await files.edit_text(WorkspaceEditRequest("note", "a", "A", original.version))
        # Unchanged mixed newline bytes and the BOM remain intact.
        self.assertEqual((await files.read_text(WorkspaceReadRequest("note"))).text, "A\r\nb\nc\r\n")
        self.assertEqual(result.bytes_written, len(raw))
        for request, code in (
            (WorkspaceEditRequest("note", "absent", "x"), "no_match"),
            (WorkspaceEditRequest("ambiguous", "aa", "x"), "ambiguous_match"),
            (WorkspaceEditRequest("note", "A", "x", original.version), "conflict"),
            (WorkspaceEditRequest("missing", "a", "x"), "not_found"),
        ):
            with self.subTest(code=code), self.assertRaises(WorkspaceFileError) as caught:
                await files.edit_text(request)
            self.assertEqual(caught.exception.code, code)

    async def test_permission_size_and_concurrent_version_conflict(self):
        with self.assertRaises(WorkspaceFileError) as caught:
            await InMemoryWorkspaceFiles({}).write_text(WorkspaceWriteRequest("x", "x"))
        self.assertEqual(caught.exception.code, "permission_denied")
        files = InMemoryWorkspaceFiles({"x": b"base"}, allow_write=True, limits=WorkspaceReadLimits(max_file_bytes=8))
        revision = (await files.read_text(WorkspaceReadRequest("x"))).version
        with self.assertRaises(WorkspaceFileError) as caught:
            await files.write_text(WorkspaceWriteRequest("x", "a" * 9, revision))
        self.assertEqual(caught.exception.code, "too_large")
        results = await asyncio.gather(*(
            files.write_text(WorkspaceWriteRequest("x", content, revision)) for content in ("first", "second")
        ), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, WorkspaceFileError) and r.code == "conflict" for r in results), 1)

    def test_mutation_and_query_request_validation(self):
        for factory in (
            lambda: WorkspaceWriteRequest("../x", "x"),
            lambda: WorkspaceWriteRequest("x", "x", "bad"),
            lambda: WorkspaceWriteRequest("x", "\x00"),
            lambda: WorkspaceEditRequest("x", "", "new"),
            lambda: WorkspaceQueryRequest("grep", pattern=""),
            lambda: WorkspaceQueryRequest("find", "../x", "*"),
            lambda: WorkspaceQueryLimits(max_entries=0),
        ):
            with self.assertRaises(ValueError):
                factory()


class QueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_glob_path_segments_and_recursive_star(self):
        files = InMemoryWorkspaceFiles({"root.py": b"", "a/x.py": b"", "a/deep/y.py": b""})
        direct = await files.query(WorkspaceQueryRequest("find", pattern="a/*.py"))
        recursive = await files.query(WorkspaceQueryRequest("find", pattern="**/*.py"))
        self.assertEqual([e.path for e in direct.entries], ["a/x.py"])
        self.assertEqual({e.path for e in recursive.entries}, {"root.py", "a/x.py", "a/deep/y.py"})

    async def test_listing_globs_literal_grep_excludes_and_skips(self):
        files = InMemoryWorkspaceFiles({"b.txt": b"TODO\nother", "a/main.py": b"TODO one\nfoo.*\nfoobar",
            ".git/config": b"TODO secret", "binary": b"\xff"})
        listed = await files.query(WorkspaceQueryRequest("ls"))
        self.assertEqual([e.path for e in listed.entries], [".git", "a", "b.txt", "binary"])
        found = await files.query(WorkspaceQueryRequest("find", pattern="*.py"))
        self.assertEqual([e.path for e in found.entries], ["a/main.py"])
        matches = await files.query(WorkspaceQueryRequest("grep", pattern="TODO"))
        self.assertEqual([(m.path, m.line) for m in matches.matches], [("a/main.py", 1), ("b.txt", 1)])
        self.assertEqual(matches.skipped_files, 1)
        literal = await files.query(WorkspaceQueryRequest("grep", "a/main.py", "foo.*"))
        self.assertEqual([m.line for m in literal.matches], [2])
        self.assertEqual((await files.query(WorkspaceQueryRequest("find", pattern="a/*.py"))).entries, found.entries)

    async def test_result_traversal_byte_depth_and_line_budgets(self):
        catalog = {"a": b"hit one\nhit two\n", "b": b"hit", "d/nested": b"hit"}
        for limits in (
            WorkspaceQueryLimits(max_entries=1), WorkspaceQueryLimits(max_output_bytes=1),
            WorkspaceQueryLimits(max_scan_bytes=1),
            WorkspaceQueryLimits(max_depth=1),
        ):
            result = await InMemoryWorkspaceFiles(catalog, query_limits=limits).query(WorkspaceQueryRequest("grep", pattern="hit"))
            self.assertTrue(result.truncated)
        with self.assertRaises(WorkspaceFileError) as caught:
            await InMemoryWorkspaceFiles(catalog, query_limits=WorkspaceQueryLimits(max_scan_entries=1)).query(WorkspaceQueryRequest("ls"))
        self.assertEqual(caught.exception.code, "scan_limit")
        counted = await InMemoryWorkspaceFiles({"a/one": b"hit", "b/two": b"hit"},
            query_limits=WorkspaceQueryLimits(max_scan_entries=3)).query(WorkspaceQueryRequest("grep", pattern="hit"))
        self.assertEqual(counted.scanned_entries, 3)
        self.assertTrue(counted.truncated)
        self.assertEqual([m.path for m in counted.matches], ["a/one"])
        result = await InMemoryWorkspaceFiles({"a": b"hit long line"}, query_limits=WorkspaceQueryLimits(max_match_chars=3)).query(WorkspaceQueryRequest("grep", pattern="hit"))
        self.assertEqual(result.matches[0].text, "hit")
        self.assertTrue(result.matches[0].truncated)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_six_tools_reuse_pipeline_and_mutations_require_approval(self):
        files = InMemoryWorkspaceFiles({"seed.py": b"TODO old\n"}, allow_write=True)
        revision = (await files.read_text(WorkspaceReadRequest("seed.py"))).version
        provider = FakeProvider([
            [ToolCall("ls", "ls", {}), ToolCall("find", "find", {"pattern": "*.py"}), ToolCall("grep", "grep", {"pattern": "TODO"}), Finish("tool_calls")],
            [ToolCall("write", "write", {"path": "new.txt", "content": "created"}), Finish("tool_calls")],
            [ToolCall("edit", "edit", {"path": "seed.py", "old_text": "old", "new_text": "new", "expected_version": revision}), Finish("tool_calls")],
            [ToolCall("read", "read", {"path": "seed.py"}), Finish("tool_calls")], [Finish()],
        ])
        policy = StaticApprovalPolicy(True)
        tools = (ReadTool(files), WriteTool(files), EditTool(files), LsTool(files), FindTool(files), GrepTool(files))
        async with await AgentRuntimeBuilder().with_provider(provider).with_profile(coding_profile()).with_tools(tools).with_approval_policy(policy).build() as runtime:
            result = await runtime.run("s", "work")
        outputs = [m for m in result.messages if m.role == "tool"]
        self.assertEqual(len(outputs), 6)
        self.assertFalse(any(m.is_error for m in outputs))
        self.assertEqual(outputs[-1].content, "TODO new\n")
        self.assertEqual([c.call.name for c in policy.requests], ["write", "edit"])

    async def test_read_only_profile_and_denied_approval_never_mutate(self):
        calls = []
        class Counted(InMemoryWorkspaceFiles):
            async def write_text(self, request):
                calls.append(request)
                return await super().write_text(request)
        files = Counted({"x": b"old"}, allow_write=True)
        revision = (await files.read_text(WorkspaceReadRequest("x"))).version
        for profile, approve in ((read_only_profile(), True), (coding_profile(), False)):
            provider = FakeProvider([[ToolCall("w", "write", {"path": "x", "content": "new", "expected_version": revision}), Finish("tool_calls")], [Finish()]])
            async with await AgentRuntimeBuilder().with_provider(provider).with_profile(profile).with_tools((WriteTool(files),)).with_approval_policy(StaticApprovalPolicy(approve)).build() as runtime:
                result = await runtime.run("s", "write")
            self.assertTrue(result.messages[2].is_error)
            self.assertEqual((await files.read_text(WorkspaceReadRequest("x"))).text, "old")
            self.assertEqual(calls, [])


class MutationDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_data_uses_stdin_and_malformed_or_timeout_outcome_is_unknown_once(self):
        for error in (None, SandboxTimeoutError("private")):
            requests = []
            class Sandbox:
                async def run(self, request):
                    if json.loads(request.stdin)["operation"] == "cleanup":
                        return SandboxProcessResult(0, '{"cleaned":true}', "", "full")
                    requests.append(request)
                    if error:
                        raise error
                    return SandboxProcessResult(0, '{"invalid":"private"}', "", "full")
            files = SandboxedWorkspaceFiles(Sandbox(), allow_write=True)
            provider = FakeProvider([[ToolCall("c", "write", {"path": "new", "content": "PRIVATE-CONTENT-" * 10000}), Finish("tool_calls")], [Finish()]])
            result = await Agent(coding_profile().build_components(provider).replace(tools=(WriteTool(files),), approval_policy=StaticApprovalPolicy(True))).run("write")
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].mode, "workspace-write")
            self.assertIn(b"PRIVATE-CONTENT-", requests[0].stdin)
            self.assertNotIn("PRIVATE-CONTENT-", " ".join(requests[0].argv))
            self.assertEqual(result.messages[2].structured_content["code"], "outcome_unknown")

    async def test_cancel_keeps_mutation_lock_until_backend_cleanup_finishes(self):
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests, cleanups = [], []
        class Sandbox:
            async def run(self, request):
                if json.loads(request.stdin)["operation"] == "cleanup":
                    cleanups.append(request)
                    return SandboxProcessResult(0, '{"cleaned":true}', "", "full")
                requests.append(request)
                if len(requests) == 1:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleanup.set()
                        await release.wait()
                return SandboxProcessResult(0, json.dumps({"path": "second", "version": "sha256:" + "0" * 64,
                    "bytes_written": 1, "created": True, "diff": "", "diff_truncated": False}), "", "full")
        files = SandboxedWorkspaceFiles(Sandbox(), allow_write=True)
        first = asyncio.create_task(files.write_text(WorkspaceWriteRequest("first", "a")))
        await entered.wait()
        first.cancel()
        await cleanup.wait()
        second = asyncio.create_task(files.write_text(WorkspaceWriteRequest("second", "b")))
        await asyncio.sleep(0)
        self.assertEqual(len(requests), 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(cleanups), 1)


@unittest.skipUnless(os.environ.get("PI_AGENT_DOCKER_IMAGE"), "requires an explicitly selected local Python image")
class DockerWorkspaceToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mutations_large_stdin_queries_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_bytes(b"\xef\xbb\xbfalpha\r\nbeta\n")
            sandbox = DockerProcessSandbox(root, image=os.environ["PI_AGENT_DOCKER_IMAGE"], max_output_bytes=2_100_000)
            files = SandboxedWorkspaceFiles(sandbox, allow_write=True)
            created = await files.write_text(WorkspaceWriteRequest("sub/large.txt", "large " * 12000))
            self.assertTrue(created.created)
            self.assertEqual((root / "sub/large.txt").read_text(), "large " * 12000)
            with self.assertRaises(WorkspaceFileError) as caught:
                await files.write_text(WorkspaceWriteRequest("sub/large.txt", "overwrite"))
            self.assertEqual(caught.exception.code, "conflict")
            initial = await files.read_text(WorkspaceReadRequest("note.txt"))
            edited = await files.edit_text(WorkspaceEditRequest("note.txt", "alpha", "ALPHA", initial.version))
            self.assertEqual((root / "note.txt").read_bytes(), b"\xef\xbb\xbfALPHA\r\nbeta\n")
            with self.assertRaises(WorkspaceFileError) as caught:
                await files.write_text(WorkspaceWriteRequest("note.txt", "stale", initial.version))
            self.assertEqual(caught.exception.code, "conflict")
            replaced = await files.write_text(WorkspaceWriteRequest("note.txt", "complete rewrite\n", edited.version))
            self.assertFalse(replaced.created)
            for operation, pattern in (("ls", None), ("find", "*.txt"), ("grep", "rewrite")):
                result = await files.query(WorkspaceQueryRequest(operation, pattern=pattern))
                self.assertTrue(result.entries or result.matches)
            self.assertEqual((await files.query(WorkspaceQueryRequest("grep", "note.txt", "rewrite"))).matches[0].line, 1)
            limited = SandboxedWorkspaceFiles(sandbox, query_limits=WorkspaceQueryLimits(max_scan_bytes=1))
            self.assertTrue((await limited.query(WorkspaceQueryRequest("grep", "note.txt", "rewrite"))).truncated)
            setup = await sandbox.run(SandboxProcessRequest(("python3", "-I", "-c",
                "import os; os.symlink('/etc/passwd','/workspace/link'); os.symlink('/tmp','/workspace/alias')")))
            self.assertEqual(setup.exit_code, 0)
            for request in (WorkspaceWriteRequest("link", "bad", initial.version), WorkspaceWriteRequest("alias/escaped", "bad")):
                with self.assertRaises(WorkspaceFileError) as caught:
                    await files.write_text(request)
                self.assertEqual(caught.exception.code, "symlink_denied")
            self.assertFalse(list(root.rglob(".pi-agent-*.tmp")))

    async def test_cleanup_after_lost_result_keeps_committed_file_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".pi-agent-unrelated.tmp").write_bytes(b"keep")
            actual = DockerProcessSandbox(root, image=os.environ["PI_AGENT_DOCKER_IMAGE"])
            operations = []
            class LoseResult:
                async def run(self, request):
                    payload = json.loads(request.stdin)
                    operations.append(payload["operation"])
                    result = await actual.run(request)
                    if payload["operation"] == "write":
                        self_test.assertEqual(result.exit_code, 0)
                        await actual.run(SandboxProcessRequest(("python3", "-I", "-c",
                            "import pathlib,sys; pathlib.Path('/workspace',sys.argv[1]).write_bytes(b'pending')", payload["temporary"])))
                        return SandboxProcessResult(0, "lost response", "", "full")
                    return result
            self_test = self
            files = SandboxedWorkspaceFiles(LoseResult(), allow_write=True)
            with self.assertRaises(WorkspaceFileError) as caught:
                await files.write_text(WorkspaceWriteRequest("committed.txt", "committed"))
            self.assertEqual(caught.exception.code, "outcome_unknown")
            self.assertEqual(operations, ["write", "cleanup"])
            self.assertEqual((root / "committed.txt").read_bytes(), b"committed")
            self.assertEqual([p.name for p in root.glob(".pi-agent-*.tmp")], [".pi-agent-unrelated.tmp"])
