"""Offline contracts for task projection, persistent environments and evidence."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pi_agent_py.application.evaluation.cli import demo, main, prepare
from pi_agent_py.application.evaluation.docker import CommandResult, DockerTaskEnvironment, run_command
from pi_agent_py.application.evaluation.runner import run_task
from pi_agent_py.application.evaluation.tasks import CodingTask
from pi_agent_py.domain import Finish, ModelRequestTarget, ShellExecRequest, TextDelta
from pi_agent_py.errors import SandboxUnavailableError
from pi_agent_py.testing import FakeProvider


def task():
    return CodingTask("demo__project-1", "demo/project", "a" * 40, "Fix the bug.", "fixture:local")


class ProjectionTests(unittest.TestCase):
    def test_grader_uses_pinned_local_data_and_explicit_gold_or_prediction(self):
        from types import SimpleNamespace
        for mode in (["--gold"], ["--predictions", "prediction.jsonl"]):
            with self.subTest(mode=mode), patch("sys.argv", ["eval", "grade", "--dataset", "grader-data.json",
                "--instance-id", task().instance_id, "--run-id", "unique", *mode]), patch(
                "pi_agent_py.application.evaluation.cli.subprocess.run", return_value=SimpleNamespace(returncode=0),
            ) as run:
                self.assertEqual(main(), 0)
                argv = run.call_args.args[0]
                self.assertIn(str(Path("grader-data.json").resolve()), argv)
                self.assertIn("swebench.harness.run_evaluation", argv)
                if "--gold" in mode:
                    self.assertEqual(argv[argv.index("--predictions_path") + 1], "gold")

    def test_gold_fields_are_excluded_and_shell_inputs_are_validated(self):
        row = {**task().__dict__, "patch": "SECRET-GOLD", "test_patch": "SECRET-TEST"}
        projected = CodingTask.from_row(row)
        self.assertNotIn("SECRET", repr(projected))
        for key, value in (("base_commit", "HEAD; bad"), ("instance_id", "../../file"), ("image", "--privileged")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                CodingTask.from_row({**row, key: value})

    def test_prepare_pins_revision_and_separates_grader_data(self):
        import types
        row = {**task().__dict__, "patch": "SECRET-GOLD", "test_patch": "SECRET-TEST"}
        calls = []
        def load(*args, **kwargs):
            calls.append((args, kwargs))
            return [row]
        with tempfile.TemporaryDirectory() as root, patch.dict("sys.modules", {"datasets": types.SimpleNamespace(load_dataset=load)}):
            out = Path(root) / "selected"
            prepare("b" * 40, "dev", [task().instance_id], out)
            self.assertEqual(calls[0][1]["revision"], "b" * 40)
            self.assertNotIn("SECRET", (out / "tasks.json").read_text())
            self.assertIn("SECRET-GOLD", (out / "grader-data.json").read_text())
            with self.assertRaises(FileExistsError):
                prepare("b" * 40, "dev", [task().instance_id], out)
        with self.assertRaises(ValueError):
            prepare("main", "dev", [task().instance_id], Path("unused"))


class ScriptedDocker:
    def __init__(self):
        self.calls = []
        self.command_error = None
        self.diff = CommandResult(0, "diff --git a/a b/a\n")

    async def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[1:3] == ("image", "inspect"):
            return CommandResult(0, "sha256:" + "a" * 64)
        if "git diff --binary " in argv[-1]:
            return self.diff
        if "conda activate" in argv[-1]:
            if self.command_error:
                raise self.command_error
            return CommandResult(1, "failure is visible to the model")
        return CommandResult(0)


class DockerEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_identity_no_host_mount_and_real_nonzero_exit(self):
        docker = ScriptedDocker()
        env = DockerTaskEnvironment(runner=docker)
        identity = await env.start(task())
        self.assertTrue(identity.startswith("sha256:"))
        create = next(c for c in docker.calls if c[1] == "create")
        self.assertNotIn("--mount", create)
        self.assertNotIn("--privileged", create)
        self.assertEqual(create[create.index("--network") + 1], "none")
        for _ in range(2):
            result = await env.execute(env.resolve(ShellExecRequest("pytest")))
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.sandbox.enforcement, "partial")
        self.assertEqual(sum(c[1] == "create" for c in docker.calls), 1)
        self.assertIn("diff --git", await env.export_patch())
        self.assertTrue(docker.calls[-1][-1].endswith(task().base_commit))
        for path in ("../escape", "/etc", "..\\escape"):
            with self.assertRaises(ValueError):
                env.resolve(ShellExecRequest("pwd", path))
        await env.close()
        await env.close()
        self.assertEqual(sum(c[1] == "rm" for c in docker.calls), 1)

    async def test_timeout_and_cancel_remove_container_and_refuse_continuation(self):
        for error in (TimeoutError(), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                docker = ScriptedDocker()
                env = DockerTaskEnvironment(runner=docker)
                await env.start(task())
                docker.command_error = error
                with self.assertRaises(type(error)):
                    await env.execute(env.resolve(ShellExecRequest("run forever")))
                self.assertEqual(docker.calls[-1][1:3], ("rm", "--force"))
                with self.assertRaises(SandboxUnavailableError):
                    await env.export_patch()

    async def test_failed_start_cleans_and_truncated_patch_is_rejected(self):
        docker = ScriptedDocker()
        env = DockerTaskEnvironment(runner=docker)
        await env.start(task())
        docker.diff = CommandResult(0, "partial diff", truncated=True)
        with self.assertRaises(SandboxUnavailableError):
            await env.export_patch()
        await env.close()
        async def fail_create(argv, timeout):
            if argv[1] == "create":
                return CommandResult(125)
            return await docker(argv, timeout)
        env = DockerTaskEnvironment(runner=fail_create)
        with self.assertRaises(SandboxUnavailableError):
            await env.start(task())
        self.assertEqual(docker.calls[-1][1], "rm")


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_records_timeout_without_starting_model(self):
        class Expired:
            async def __aenter__(self):
                raise TimeoutError()
            async def __aexit__(self, *args):
                pass
        docker = ScriptedDocker()
        provider = FakeProvider([[Finish()]])
        with tempfile.TemporaryDirectory() as root, patch(
            "pi_agent_py.application.evaluation.runner.asyncio.timeout", return_value=Expired(),
        ):
            report = await run_task(task(), DockerTaskEnvironment(runner=docker), provider,
                                    ModelRequestTarget("fake", "test"), Path(root) / "timeout",
                                    source={}, code_commit="fixture")
            self.assertEqual(report["status"], "timeout")
            self.assertFalse(provider.requests)

    async def test_demo_persists_tool_continuation_inputs_and_ungraded_prediction(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "demo"
            await demo(out)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["status"], "completed")
            self.assertIsNone(report["resolved"])
            self.assertEqual(report["event_counts"]["model_request_started"], 2)
            self.assertEqual(len(list((out / "requests").glob("*.json"))), 2)
            self.assertIn("synthetic fix", (out / "session.jsonl").read_text())
            self.assertEqual(json.loads((out / "predictions.jsonl").read_text())["model_patch"], (out / "patch.diff").read_text())

    async def test_environment_failure_is_reported_and_closed_without_prediction(self):
        class BrokenEnvironment:
            closed = False
            async def start(self, task):
                raise SandboxUnavailableError("private diagnostic")
            async def close(self):
                self.closed = True
        env = BrokenEnvironment()
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "failure"
            report = await run_task(task(), env, FakeProvider([]), ModelRequestTarget("fake", "test"),
                                    out, source={}, code_commit="fixture")
            self.assertEqual(report["status"], "infrastructure_error")
            self.assertTrue(env.closed)
            self.assertFalse((out / "predictions.jsonl").exists())
            self.assertNotIn("private diagnostic", (out / "report.json").read_text())

    async def test_cancel_model_closes_environment_and_writes_terminal_evidence(self):
        from pi_agent_py.testing import WaitUntilCancelled
        docker = ScriptedDocker()
        env = DockerTaskEnvironment(runner=docker)
        waiting = WaitUntilCancelled()
        provider = FakeProvider([[waiting]])
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "cancel"
            running = asyncio.create_task(run_task(task(), env, provider, ModelRequestTarget("fake", "test"),
                                                   out, source={}, code_commit="fixture"))
            await provider.blocked.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["status"], "cancelled")
            self.assertEqual(docker.calls[-1][1], "rm")
            self.assertFalse((out / "predictions.jsonl").exists())

    async def test_runtime_failure_keeps_partial_patch_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "model-error"
            report = await run_task(task(), DockerTaskEnvironment(runner=ScriptedDocker()),
                                    FakeProvider([[TextDelta("incomplete")]]), ModelRequestTarget("fake", "test"),
                                    out, source={}, code_commit="fixture")
            self.assertEqual(report["status"], "runtime_error")
            self.assertIsNone(report["resolved"])
            self.assertTrue((out / "predictions.jsonl").exists())


@unittest.skipUnless(os.environ.get("PI_AGENT_EVAL_PROCESS_INTEGRATION") == "1", "explicit process integration")
class ProcessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_process_drains_both_streams_with_bounded_capture(self):
        result = await run_command((sys.executable, "-c",
            "import sys; sys.stdout.write('x'*5000100); sys.stderr.write('diagnostic'); sys.exit(7)"), 30)
        self.assertEqual(result.code, 7)
        self.assertEqual(len(result.stdout), 5_000_000)
        self.assertEqual(result.stderr, "diagnostic")
        self.assertTrue(result.truncated)


@unittest.skipUnless(os.environ.get("PI_AGENT_SWEBENCH_IMAGE"), "explicit benchmark image integration")
class BenchmarkImageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_image_setup_and_shared_files(self):
        from dataclasses import replace
        selected = replace(task(), instance_id=os.environ["PI_AGENT_SWEBENCH_ID"],
                           repo=os.environ["PI_AGENT_SWEBENCH_REPO"],
                           base_commit=os.environ["PI_AGENT_SWEBENCH_COMMIT"],
                           image=os.environ["PI_AGENT_SWEBENCH_IMAGE"])
        env = DockerTaskEnvironment()
        try:
            await env.start(selected)
            await env.execute(env.resolve(ShellExecRequest("printf persisted > .pi-eval-probe")))
            result = await env.execute(env.resolve(ShellExecRequest("cat .pi-eval-probe")))
            self.assertEqual(result.stdout.text, "persisted")
            self.assertIn(".pi-eval-probe", await env.export_patch())
        finally:
            await env.close()
