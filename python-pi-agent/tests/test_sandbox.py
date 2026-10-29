from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable
from pathlib import Path

from pi_agent_py import (
    Agent,
    DeclaredSideEffectGate,
    DockerProcessSandbox,
    FakeProvider,
    Finish,
    JSONObject,
    SandboxProcessRequest,
    SandboxProcessResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
    SandboxedShellTool,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
    ToolExecutionContext,
)


class FakeReader:
    def __init__(
        self,
        data: bytes,
        *,
        block: bool,
        started: asyncio.Event,
    ) -> None:
        self._data = data
        self._block = block
        self._started = started

    async def read(self, count: int = -1) -> bytes:
        del count
        self._started.set()
        if self._block:
            await asyncio.Event().wait()
        data, self._data = self._data, b""
        return data


class FakeProcess:
    def __init__(
        self,
        *,
        returncode: int | None = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        block: bool = False,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._block = block
        self.started = asyncio.Event()
        self.killed = False
        self.stdout = FakeReader(stdout, block=block, started=self.started)
        self.stderr = FakeReader(stderr, block=block, started=self.started)

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        if self._block:
            await asyncio.Event().wait()
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class QueueLauncher:
    def __init__(self, *processes: FakeProcess) -> None:
        self._processes = list(processes)
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str] | None] = []

    def __call__(
        self,
        *args: str,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> Awaitable[FakeProcess]:
        del stdout, stderr
        self.calls.append(args)
        self.environments.append(env)
        if not self._processes:
            raise AssertionError("unexpected process launch")

        async def launch() -> FakeProcess:
            return self._processes.pop(0)

        return launch()


class RecordingSandbox:
    def __init__(self, result: SandboxProcessResult) -> None:
        self.result = result
        self.requests: list[SandboxProcessRequest] = []

    async def run(self, request: SandboxProcessRequest) -> SandboxProcessResult:
        self.requests.append(request)
        return self.result


class DockerProcessSandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)

    def test_builds_read_only_workspace_with_no_network_or_host_environment(
        self,
    ) -> None:
        sandbox = DockerProcessSandbox(
            self.workspace,
            image="local/python-agent:test",
            environment={"SAFE_FLAG": "1"},
            launcher=QueueLauncher(),
        )

        arguments = sandbox.build_command(
            SandboxProcessRequest(("python", "-V"), mode="read-only"),
            container_name="sandbox-test",
        )

        mount = arguments[arguments.index("--mount") + 1]
        self.assertIn(str(self.workspace.resolve()), mount)
        self.assertTrue(mount.endswith(",readonly"))
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn("no-new-privileges", arguments)
        self.assertIn("ALL", arguments)
        self.assertIn("SAFE_FLAG", arguments)
        self.assertNotIn("SAFE_FLAG=1", arguments)
        self.assertNotIn("PATH=", " ".join(arguments))
        self.assertEqual(arguments[-2:], ("python", "-V"))

    def test_request_rejects_ambiguous_argv_workdir_and_timeout(self) -> None:
        invalid_requests = (
            lambda: SandboxProcessRequest([]),
            lambda: SandboxProcessRequest((" ",)),
            lambda: SandboxProcessRequest(("echo", "bad\x00arg")),
            lambda: SandboxProcessRequest(("pwd",), workdir="/tmp"),
            lambda: SandboxProcessRequest(
                ("pwd",),
                workdir="/workspace/../tmp",
            ),
            lambda: SandboxProcessRequest(("sleep", "1"), timeout_seconds=True),
            lambda: SandboxProcessRequest(
                ("sleep", "1"),
                timeout_seconds=float("inf"),
            ),
        )
        for make_request in invalid_requests:
            with self.subTest(make_request=make_request):
                with self.assertRaises((TypeError, ValueError)):
                    make_request()

    def test_workspace_write_changes_only_the_workspace_mount_mode(self) -> None:
        sandbox = DockerProcessSandbox(
            self.workspace,
            image="local/python-agent:test",
            launcher=QueueLauncher(),
        )

        arguments = sandbox.build_command(
            SandboxProcessRequest(
                ("touch", "result.txt"),
                workdir="/workspace/subdir",
            ),
            container_name="sandbox-test",
        )

        mount = arguments[arguments.index("--mount") + 1]
        self.assertNotIn("readonly", mount)
        self.assertIn("--read-only", arguments)
        self.assertEqual(
            arguments[arguments.index("--workdir") + 1],
            "/workspace/subdir",
        )

    async def test_returns_enforcement_evidence_and_explicit_environment(
        self,
    ) -> None:
        process = FakeProcess(stdout=b"12345", stderr=b"67890")
        launcher = QueueLauncher(process)
        sandbox = DockerProcessSandbox(
            self.workspace,
            image="local/python-agent:test",
            environment={"SAFE_FLAG": "1"},
            max_output_bytes=10,
            launcher=launcher,
        )

        result = await sandbox.run(SandboxProcessRequest(("run", "tests")))

        self.assertEqual(result.stdout, "12345")
        self.assertEqual(result.stderr, "67890")
        self.assertFalse(result.output_truncated)
        self.assertEqual(result.enforcement, "full")
        launched_environment = launcher.environments[0]
        self.assertIsNotNone(launched_environment)
        assert launched_environment is not None
        self.assertEqual(launched_environment["SAFE_FLAG"], "1")
        self.assertNotIn("SAFE_FLAG=1", launcher.calls[0])

    async def test_output_limit_retains_bounded_stream_tails(self) -> None:
        process = FakeProcess(stdout=b"12345", stderr=b"67890")
        launcher = QueueLauncher(process)
        sandbox = DockerProcessSandbox(
            self.workspace,
            image="local/python-agent:test",
            max_output_bytes=3,
            launcher=launcher,
        )

        result = await sandbox.run(
            SandboxProcessRequest(("print", "a-lot"))
        )

        self.assertEqual(result.stdout, "345")
        self.assertEqual(result.stderr, "890")
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        self.assertTrue(result.output_truncated)
        self.assertFalse(process.killed)
        self.assertEqual(len(launcher.calls), 1)

    async def test_docker_runtime_failure_is_not_treated_as_command_failure(
        self,
    ) -> None:
        launcher = QueueLauncher(
            FakeProcess(returncode=125, stderr=b"daemon unavailable")
        )
        sandbox = DockerProcessSandbox(
            self.workspace,
            image="local/python-agent:test",
            launcher=launcher,
        )

        with self.assertRaises(SandboxUnavailableError):
            await sandbox.run(
                SandboxProcessRequest(("echo", "never-ran"))
            )

    async def test_timeout_force_removes_container_and_kills_client(self) -> None:
        process = FakeProcess(returncode=None, block=True)
        cleanup = FakeProcess()
        launcher = QueueLauncher(process, cleanup)
        sandbox = DockerProcessSandbox(
            self.workspace,
            image="local/python-agent:test",
            launcher=launcher,
        )

        with self.assertRaises(SandboxTimeoutError):
            await sandbox.run(
                SandboxProcessRequest(("block",), timeout_seconds=0)
            )

        self.assertTrue(process.killed)
        container_name = launcher.calls[0][
            launcher.calls[0].index("--name") + 1
        ]
        self.assertEqual(
            launcher.calls[1],
            ("docker", "rm", "--force", container_name),
        )

    async def test_cancellation_force_removes_container_and_propagates(self) -> None:
        process = FakeProcess(returncode=None, block=True)
        cleanup = FakeProcess()
        launcher = QueueLauncher(process, cleanup)
        sandbox = DockerProcessSandbox(
            self.workspace,
            image="local/python-agent:test",
            launcher=launcher,
        )
        task = asyncio.create_task(
            sandbox.run(SandboxProcessRequest(("block",)))
        )
        await process.started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(process.killed)
        self.assertEqual(launcher.calls[1][1:3], ("rm", "--force"))


class SandboxedShellToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_runs_shell_through_gate_approval_and_sandbox(self) -> None:
        sandbox = RecordingSandbox(
            SandboxProcessResult(
                exit_code=0,
                stdout="tests passed",
                stderr="",
                enforcement="full",
            )
        )
        shell = SandboxedShellTool(sandbox)
        provider = FakeProvider(
            [
                [
                    ToolCall("call-agent", "shell", {"command": "pytest -q"}),
                    Finish("tool_calls"),
                ],
                [TextDelta("done"), Finish()],
            ]
        )
        agent = Agent(
            provider,
            tools=(shell,),
            approval_policy=StaticApprovalPolicy(True),
            execution_gate=DeclaredSideEffectGate(
                allowed_side_effects=("none", "write"),
                allowed_tools=("shell",),
            ),
        )

        result = await agent.run("run tests")

        self.assertEqual(result.messages[2].content, "tests passed")
        self.assertEqual(result.message.content, "done")
        self.assertEqual(
            sandbox.requests[0].argv,
            ("/bin/sh", "-lc", "pytest -q"),
        )

    async def test_shell_tool_delegates_to_process_sandbox(self) -> None:
        sandbox = RecordingSandbox(
            SandboxProcessResult(
                exit_code=0,
                stdout="ok\n",
                stderr="",
                enforcement="full",
            )
        )
        tool = SandboxedShellTool(
            sandbox,
            mode="read-only",
            timeout_seconds=30,
            requires_approval=False,
        )
        context = ToolExecutionContext(
            call=ToolCall("call-1", "shell", {"command": "pytest -q"}),
            definition=tool.definition,
            request_number=1,
            call_index=1,
        )

        output = await tool.execute(
            {"command": "pytest -q"},
            context,
        )

        self.assertEqual(output.content, "ok\n")
        self.assertFalse(output.is_error)
        self.assertEqual(sandbox.requests[0].mode, "read-only")
        self.assertEqual(sandbox.requests[0].timeout_seconds, 30)
        self.assertEqual(output.structured_content["enforcement"], "full")

    async def test_shell_tool_rejects_partial_enforcement_by_default(self) -> None:
        sandbox = RecordingSandbox(
            SandboxProcessResult(
                exit_code=0,
                stdout="",
                stderr="",
                enforcement="partial",
            )
        )
        tool = SandboxedShellTool(sandbox, requires_approval=False)
        arguments: JSONObject = {"command": "echo unsafe"}
        context = ToolExecutionContext(
            call=ToolCall("call-2", "shell", arguments),
            definition=tool.definition,
            request_number=1,
            call_index=1,
        )

        with self.assertRaises(SandboxUnavailableError):
            await tool.execute(arguments, context)


if __name__ == "__main__":
    unittest.main()
