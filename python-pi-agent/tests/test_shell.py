from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from pi_agent_py import (
    Agent,
    BashTool,
    DeclaredSideEffectGate,
    FakeProvider,
    Finish,
    SandboxProcessRequest,
    SandboxProcessResult,
    SandboxTimeoutError,
    SandboxUnavailableError,
    ShellCollectedOutput,
    ShellExecRequest,
    ShellExecSpec,
    ShellRunResult,
    ShellSandboxInfo,
    SandboxedBashExecutor,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
    ToolExecutionContext,
)


class RecordingShellExecutor:
    def __init__(self, result: ShellRunResult) -> None:
        self.result = result
        self.requests: list[ShellExecRequest] = []
        self.specs: list[ShellExecSpec] = []

    def resolve(self, request: ShellExecRequest) -> ShellExecSpec:
        self.requests.append(request)
        timeout_seconds = min(request.timeout_seconds or 120.0, 600.0)
        return ShellExecSpec(
            command=request.command,
            workdir=request.workdir or "/workspace",
            timeout_seconds=timeout_seconds,
        )

    async def execute(self, spec: ShellExecSpec) -> ShellRunResult:
        self.specs.append(spec)
        return replace(self.result, timeout_seconds=spec.timeout_seconds)


class RecordingProcessSandbox:
    def __init__(
        self,
        result: SandboxProcessResult | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.requests: list[SandboxProcessRequest] = []

    async def run(self, request: SandboxProcessRequest) -> SandboxProcessResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def completed_result(
    *,
    exit_code: int | None = 0,
    stdout: ShellCollectedOutput | None = None,
    stderr: ShellCollectedOutput | None = None,
    signal: str | None = None,
    timed_out: bool = False,
    cancelled: bool = False,
    sandbox: ShellSandboxInfo | None = None,
) -> ShellRunResult:
    return ShellRunResult(
        exit_code=exit_code,
        signal=signal,
        timed_out=timed_out,
        cancelled=cancelled,
        timeout_seconds=120.0,
        stdout=stdout or ShellCollectedOutput(""),
        stderr=stderr or ShellCollectedOutput(""),
        sandbox=sandbox,
    )


class BashToolTests(unittest.IsolatedAsyncioTestCase):
    def test_definition_exposes_only_model_safe_arguments(self) -> None:
        tool = BashTool(RecordingShellExecutor(completed_result()))

        properties = tool.definition.input_schema["properties"]

        self.assertEqual(
            set(properties),
            {"command", "workdir", "timeout_seconds"},
        )
        self.assertEqual(tool.definition.name, "bash")
        self.assertTrue(tool.definition.requires_approval)
        self.assertEqual(tool.definition.side_effects, "write")

    async def test_resolves_named_arguments_and_reports_nonzero_exit(self) -> None:
        executor = RecordingShellExecutor(
            completed_result(
                exit_code=2,
                stdout=ShellCollectedOutput("collected output\n"),
                stderr=ShellCollectedOutput("tests failed\n"),
                sandbox=ShellSandboxInfo(
                    mode="workspace-write",
                    enforcement="full",
                ),
            )
        )
        tool = BashTool(executor, requires_approval=False)
        arguments = {
            "command": "pytest -q",
            "workdir": "python-pi-agent",
            "timeout_seconds": 30,
        }
        context = ToolExecutionContext(
            call=ToolCall("call-1", "bash", arguments),
            definition=tool.definition,
            request_number=1,
            call_index=1,
        )

        output = await tool.execute(arguments, context)

        self.assertEqual(
            executor.requests,
            [
                ShellExecRequest(
                    command="pytest -q",
                    workdir="python-pi-agent",
                    timeout_seconds=30,
                )
            ],
        )
        self.assertEqual(executor.specs[0].workdir, "python-pi-agent")
        self.assertIn("[stderr]\ntests failed", output.content)
        self.assertIn("[exit code: 2]", output.content)
        self.assertFalse(output.is_error)
        assert isinstance(output.structured_content, dict)
        self.assertEqual(output.structured_content["exit_code"], 2)
        self.assertEqual(output.structured_content["workdir"], "python-pi-agent")
        self.assertEqual(output.structured_content["timeout_seconds"], 30)

    async def test_renders_independent_timeout_truncation_and_sandbox_facts(
        self,
    ) -> None:
        executor = RecordingShellExecutor(
            completed_result(
                exit_code=0,
                timed_out=True,
                stdout=ShellCollectedOutput("tail", truncated=True),
                sandbox=ShellSandboxInfo(
                    mode="read-only",
                    enforcement="full",
                    denied=True,
                ),
            )
        )
        tool = BashTool(executor, requires_approval=False)
        arguments = {"command": "write-and-trap"}
        context = ToolExecutionContext(
            call=ToolCall("call-2", "bash", arguments),
            definition=tool.definition,
            request_number=1,
            call_index=1,
        )

        output = await tool.execute(arguments, context)

        self.assertIn("[stdout truncated]", output.content)
        self.assertIn(
            "[sandbox: file access denied under read-only mode]",
            output.content,
        )
        self.assertIn("[timed out after 120s]", output.content)

    async def test_cancelled_execution_propagates_cancellation(self) -> None:
        executor = RecordingShellExecutor(completed_result(cancelled=True))
        tool = BashTool(executor, requires_approval=False)
        arguments = {"command": "sleep 10"}
        context = ToolExecutionContext(
            call=ToolCall("call-3", "bash", arguments),
            definition=tool.definition,
            request_number=1,
            call_index=1,
        )

        with self.assertRaises(asyncio.CancelledError):
            await tool.execute(arguments, context)

    async def test_agent_runs_bash_through_approval_gate_and_executor(self) -> None:
        executor = RecordingShellExecutor(
            completed_result(stdout=ShellCollectedOutput("tests passed\n"))
        )
        bash = BashTool(executor)
        provider = FakeProvider(
            [
                [
                    ToolCall("call-bash", "bash", {"command": "pytest -q"}),
                    Finish("tool_calls"),
                ],
                [TextDelta("done"), Finish()],
            ]
        )
        agent = Agent(
            provider,
            tools=(bash,),
            approval_policy=StaticApprovalPolicy(True),
            execution_gate=DeclaredSideEffectGate(
                allowed_side_effects=("none", "write"),
                allowed_tools=("bash",),
            ),
        )

        result = await agent.run("run tests")

        self.assertEqual(result.messages[2].content, "tests passed\n")
        self.assertEqual(result.message.content, "done")
        self.assertEqual(executor.requests[0].command, "pytest -q")


class ShellContractTests(unittest.TestCase):
    def test_request_rejects_unusable_command_workdir_and_timeout(self) -> None:
        for request in (
            lambda: ShellExecRequest(command=" "),
            lambda: ShellExecRequest(command="echo ok\x00"),
            lambda: ShellExecRequest(command="echo ok", workdir=" "),
            lambda: ShellExecRequest(command="echo ok", timeout_seconds=0),
            lambda: ShellExecRequest(command="echo ok", timeout_seconds=True),
            lambda: ShellExecRequest(command="echo ok", timeout_seconds=float("inf")),
        ):
            with self.subTest(request=request):
                with self.assertRaises((TypeError, ValueError)):
                    request()

    def test_result_rejects_ambiguous_interruption_cause(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            completed_result(timed_out=True, cancelled=True)


class SandboxedBashExecutorTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_maps_workspace_relative_workdir_and_caps_timeout(self) -> None:
        executor = SandboxedBashExecutor(
            RecordingProcessSandbox(),
            default_timeout_seconds=30,
            max_timeout_seconds=60,
        )

        spec = executor.resolve(
            ShellExecRequest(
                command="pytest -q",
                workdir="packages/core",
                timeout_seconds=90,
            )
        )

        self.assertEqual(spec.workdir, "/workspace/packages/core")
        self.assertEqual(spec.timeout_seconds, 60)

    def test_resolve_rejects_workdir_escape_or_host_path_syntax(self) -> None:
        executor = SandboxedBashExecutor(RecordingProcessSandbox())

        for workdir in ("../outside", "/tmp", "packages\\core"):
            with self.subTest(workdir=workdir):
                with self.assertRaises(ValueError):
                    executor.resolve(
                        ShellExecRequest(command="pwd", workdir=workdir)
                    )

    async def test_executes_exact_bash_argv_and_maps_bounded_output(self) -> None:
        sandbox = RecordingProcessSandbox(
            SandboxProcessResult(
                exit_code=2,
                stdout="stdout tail",
                stderr="stderr tail",
                enforcement="full",
                stdout_truncated=True,
                stderr_truncated=True,
            )
        )
        executor = SandboxedBashExecutor(
            sandbox,
            mode="read-only",
            default_timeout_seconds=45,
        )
        spec = executor.resolve(ShellExecRequest(command="pytest -q"))

        result = await executor.execute(spec)

        self.assertEqual(
            sandbox.requests,
            [
                SandboxProcessRequest(
                    argv=("/bin/bash", "-lc", "pytest -q"),
                    workdir="/workspace",
                    mode="read-only",
                    timeout_seconds=45,
                )
            ],
        )
        self.assertEqual(result.exit_code, 2)
        self.assertTrue(result.stdout.truncated)
        self.assertTrue(result.stderr.truncated)
        self.assertEqual(
            result.sandbox,
            ShellSandboxInfo(mode="read-only", enforcement="full"),
        )

    async def test_timeout_becomes_a_result_but_partial_enforcement_fails(self) -> None:
        timeout_executor = SandboxedBashExecutor(
            RecordingProcessSandbox(error=SandboxTimeoutError("expired")),
            mode="workspace-write",
        )
        timed_out = await timeout_executor.execute(
            timeout_executor.resolve(ShellExecRequest(command="sleep 10"))
        )

        self.assertTrue(timed_out.timed_out)
        self.assertIsNone(timed_out.exit_code)
        self.assertEqual(
            timed_out.sandbox,
            ShellSandboxInfo(mode="workspace-write", enforcement=None),
        )

        partial_executor = SandboxedBashExecutor(
            RecordingProcessSandbox(
                SandboxProcessResult(
                    exit_code=0,
                    stdout="",
                    stderr="",
                    enforcement="partial",
                )
            )
        )
        with self.assertRaises(SandboxUnavailableError):
            await partial_executor.execute(
                partial_executor.resolve(ShellExecRequest(command="true"))
            )

    async def test_process_sandbox_cancellation_propagates(self) -> None:
        executor = SandboxedBashExecutor(
            RecordingProcessSandbox(error=asyncio.CancelledError())
        )

        with self.assertRaises(asyncio.CancelledError):
            await executor.execute(
                executor.resolve(ShellExecRequest(command="sleep 10"))
            )


if __name__ == "__main__":
    unittest.main()
