"""Deterministic case continuation, fixed batch denominator, and grading."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from pi_agent_py.application.evaluation.batch import GradeResult, OfficialGrader, run_batch, summarize
from pi_agent_py.application.evaluation.cases import output_lock, read_record, run_case, write_record
from pi_agent_py.application.evaluation.docker import CommandResult
from pi_agent_py.application.evaluation.policy import EvaluationPolicy
from pi_agent_py.domain import Finish, ModelRequestTarget, ShellCollectedOutput, ShellExecSpec, ShellRunResult, ToolCall
from pi_agent_py.errors import ProviderRateLimitError
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled
from pi_agent_py.application.evaluation.tasks import CodingTask


POLICY = EvaluationPolicy(max_requests=4, timeout_seconds=100, min_request_interval_seconds=0,
                          retry_base_seconds=0, retry_max_seconds=0, retry_attempts=2)
TARGET = ModelRequestTarget("fake", "scripted")


def task(number=1):
    return CodingTask(f"demo__project-{number}", "demo/project", "a" * 40, f"Fix issue {number}", "fixture:local")


class Workspace:
    def __init__(self, *, image="sha256:fixture", patch="diff --git a/a b/a\n"):
        self.image = image
        self.patch = patch
        self.restored = []
        self.closed = False

    async def start(self, task):
        return self.image

    async def restore_patch(self, patch):
        self.restored.append(patch)

    def resolve(self, request):
        return ShellExecSpec(request.command, "/testbed", 1)

    async def execute(self, spec):
        return ShellRunResult(0, None, False, False, 1, ShellCollectedOutput("ok"), ShellCollectedOutput(""))

    async def export_patch(self):
        return self.patch

    async def close(self):
        self.closed = True


class CaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_limit_is_explicit_and_resume_does_not_add_requests(self):
        from dataclasses import replace
        policy = replace(POLICY, max_requests=1)
        raw = FakeProvider([[ToolCall("call", "bash", {"command": "fix"}), Finish("tool_calls")]])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "case"
            result = await run_case(task(), Workspace, raw, TARGET, out, policy=policy,
                                    source={}, code_commit="fixture", wall_clock=lambda: 1)
            self.assertEqual(result["status"], "request_limit")
            self.assertTrue((out / "predictions.jsonl").is_file())
            next_provider = FakeProvider([[Finish()]])
            result = await run_case(task(), Workspace, next_provider, TARGET, out, policy=policy,
                                    source={}, code_commit="fixture", wall_clock=lambda: 2, resume=True)
            self.assertEqual(result["status"], "budget_exhausted")
            self.assertFalse(next_provider.requests)

    async def test_retry_records_one_logical_request_and_two_physical_attempts(self):
        raw = FakeProvider([[ProviderRateLimitError("limited")], [Finish()]])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "case"
            result = await run_case(task(), Workspace, raw, TARGET, out, policy=POLICY,
                                    source={}, code_commit="fixture")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["consumed_requests"], 1)
            segment = out / "segments/segment-0001"
            self.assertEqual(len(list((segment / "requests").glob("*.json"))), 1)
            self.assertEqual(len(list((segment / "physical-requests").glob("*.json"))), 2)
            logical = [json.loads(line) for line in (segment / "trace.jsonl").read_text().splitlines()]
            attempts = [json.loads(line) for line in (segment / "resilience-trace.jsonl").read_text().splitlines()]
            self.assertEqual(sum(e["kind"] == "provider_attempt_started" for e in logical), 1)
            self.assertEqual(sum(e["kind"] == "provider_attempt_started" for e in attempts), 2)

    async def test_cancel_checkpoint_resumes_history_workspace_and_original_budget(self):
        blocked = FakeProvider([[WaitUntilCancelled()]])
        first = Workspace()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "case"
            running = asyncio.create_task(run_case(task(), lambda: first, blocked, TARGET, out,
                policy=POLICY, source={}, code_commit="fixture", wall_clock=lambda: 10))
            await blocked.blocked.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            self.assertTrue(first.closed)
            self.assertEqual(read_record(out / "report.json")["status"], "cancelled")
            deadline = read_record(out / "case.json")["deadline_at"]
            second = Workspace()
            raw = FakeProvider([[Finish()]])
            result = await run_case(task(), lambda: second, raw, TARGET, out,
                policy=POLICY, source={}, code_commit="fixture", wall_clock=lambda: 30, resume=True)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["consumed_requests"], 2)
            self.assertEqual(read_record(out / "case.json")["deadline_at"], deadline)
            self.assertEqual(second.restored, [first.patch])
            self.assertTrue(any(m.content == task().problem_statement for m in raw.requests[0]))
            self.assertEqual(len(list((out / "segments").iterdir())), 2)

    async def test_resume_rejects_configuration_or_checkpoint_mutation_before_paid_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "case"
            await run_case(task(), Workspace, FakeProvider([[ValueError("failed")]]), TARGET, out,
                           policy=POLICY, source={}, code_commit="fixture", wall_clock=lambda: 1)
            raw = FakeProvider([[Finish()]])
            with self.assertRaises(ValueError):
                await run_case(task(), Workspace, raw, TARGET, out, policy=EvaluationPolicy(),
                               source={}, code_commit="fixture", wall_clock=lambda: 2, resume=True)
            (out / "segments/segment-0001/patch.diff").write_text("tampered")
            with self.assertRaises(ValueError):
                await run_case(task(), Workspace, raw, TARGET, out, policy=POLICY,
                               source={}, code_commit="fixture", wall_clock=lambda: 2, resume=True)
            self.assertFalse(raw.requests)

    async def test_deadline_is_not_reset_and_incomplete_segment_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "case"
            await run_case(task(), Workspace, FakeProvider([[ValueError("failed")]]), TARGET, out,
                           policy=POLICY, source={}, code_commit="fixture", wall_clock=lambda: 1)
            raw = FakeProvider([[Finish()]])
            report = await run_case(task(), Workspace, raw, TARGET, out, policy=POLICY,
                                   source={}, code_commit="fixture", wall_clock=lambda: 102, resume=True)
            self.assertEqual(report["status"], "budget_exhausted")
            self.assertFalse(raw.requests)
            (out / "segments/segment-0001/report.json").unlink()
            with self.assertRaises(ValueError):
                await run_case(task(), Workspace, raw, TARGET, out, policy=POLICY,
                               source={}, code_commit="fixture", wall_clock=lambda: 103, resume=True)

    async def test_changed_image_is_rejected_before_model_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "case"
            await run_case(task(), Workspace, FakeProvider([[ValueError("failed")]]), TARGET, out,
                           policy=POLICY, source={}, code_commit="fixture", wall_clock=lambda: 1)
            raw = FakeProvider([[Finish()]])
            result = await run_case(task(), lambda: Workspace(image="sha256:changed"), raw, TARGET, out,
                policy=POLICY, source={}, code_commit="fixture", wall_clock=lambda: 2, resume=True)
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertFalse(raw.requests)
            self.assertFalse((out / "predictions.jsonl").exists())


class FakeGrader:
    def __init__(self):
        self.calls = []

    async def evaluate(self, task, prediction, output, *, gold=False):
        self.calls.append((task.instance_id, gold))
        if gold:
            return GradeResult("graded", not task.instance_id.endswith("-2"), str(output), True)
        return GradeResult("graded", task.instance_id.endswith("-1"), str(output), True)


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_grader_failure_does_not_relabel_successful_execution(self):
        class BrokenGrader(FakeGrader):
            async def evaluate(self, task, prediction, output, *, gold=False):
                if not gold:
                    raise OSError("grader unavailable")
                return await super().evaluate(task, prediction, output, gold=gold)
        with tempfile.TemporaryDirectory() as directory:
            result = await run_batch([task()], Workspace, FakeProvider([[Finish()]]), TARGET,
                BrokenGrader(), Path(directory) / "batch", policy=POLICY, source={},
                code_commit="fixture", grading_data_sha256="pinned")
            self.assertEqual(result["execution_counts"], {"completed": 1})
            self.assertEqual(result["grade_counts"], {"grader_error": 1})
            self.assertEqual(result["selected_instances"], 1)

    async def test_failed_control_stays_in_denominator_and_resume_skips_all_finished_cases(self):
        grader = FakeGrader()
        raw = FakeProvider([[Finish()], [Finish()]])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "batch"
            result = await run_batch([task(1), task(2), task(3)], Workspace, raw, TARGET, grader, out,
                policy=POLICY, source={}, code_commit="fixture", grading_data_sha256="pinned")
            self.assertEqual(result["selected_instances"], 3)
            self.assertEqual(result["resolved_instances"], 1)
            self.assertEqual(result["execution_counts"]["environment_failed"], 1)
            self.assertEqual(len(raw.requests), 2)
            empty = FakeProvider([])
            before = len(grader.calls)
            resumed = await run_batch([task(1), task(2), task(3)], Workspace, empty, TARGET, grader, out,
                policy=POLICY, source={}, code_commit="fixture", grading_data_sha256="pinned", resume=True)
            self.assertEqual(resumed, result)
            self.assertEqual(len(grader.calls), before)
            self.assertFalse(empty.requests)
            state = read_record(out / "state.json")
            del state["rows"][task(2).instance_id]
            write_record(out / "state.json", state)
            with self.assertRaises(ValueError):
                summarize(out)

    async def test_cancelled_batch_continues_case_without_replaying_completed_tools(self):
        raw = FakeProvider([[WaitUntilCancelled()]])
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "batch"
            running = asyncio.create_task(run_batch([task()], Workspace, raw, TARGET, FakeGrader(), out,
                policy=POLICY, source={}, code_commit="fixture", grading_data_sha256="pinned"))
            await raw.blocked.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            next_provider = FakeProvider([[Finish()]])
            result = await run_batch([task()], Workspace, next_provider, TARGET, FakeGrader(), out,
                policy=POLICY, source={}, code_commit="fixture", grading_data_sha256="pinned", resume=True)
            self.assertEqual(result["resolved_instances"], 1)
            self.assertEqual(result["rows"][task().instance_id]["consumed_requests"], 2)


class GraderTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_grader_removes_only_its_owned_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data.json"
            data.write_text(json.dumps([asdict(task())]))
            calls = []
            async def cancelled(argv, timeout, *, stdin=None):
                calls.append(argv)
                if argv[0] != "docker":
                    raise asyncio.CancelledError()
                return CommandResult(0)
            grader = OfficialGrader(data, "dev", runner=cancelled)
            with self.assertRaises(asyncio.CancelledError):
                await grader.evaluate(task(), None, root / "grade", gold=True)
            run_id = calls[0][calls[0].index("--run_id") + 1]
            self.assertEqual(calls[-1], ("docker", "rm", "--force", f"sweb.eval.{task().instance_id}.{run_id}"))

    async def test_exit_zero_is_not_a_resolved_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data.json"
            data.write_text(json.dumps([asdict(task())]))
            async def runner(argv, timeout, *, stdin=None):
                target = Path(argv[argv.index("--report_dir") + 1])
                run_id = argv[argv.index("--run_id") + 1]
                write_record(target / f"fake.{run_id}.json", {
                    "total_instances": 1, "submitted_ids": [task().instance_id],
                    "resolved_ids": [], "completed_ids": [], "empty_patch_instances": 1})
                return CommandResult(0)
            grader = OfficialGrader(data, "dev", runner=runner)
            result = await grader.evaluate(task(), root / "predictions.jsonl", root / "grade")
            self.assertFalse(result.resolved)
            self.assertFalse(result.tests_executed)
            self.assertEqual(result.status, "empty_patch")
            data.write_text("[]")
            with self.assertRaises(ValueError):
                await grader.evaluate(task(), None, root / "changed", gold=True)


class LockTests(unittest.TestCase):
    def test_second_writer_is_rejected_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with output_lock(root):
                with self.assertRaises(ValueError):
                    with output_lock(root):
                        self.fail("second writer acquired the same output")
            with output_lock(root):
                pass
