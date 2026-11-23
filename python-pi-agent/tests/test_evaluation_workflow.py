"""Offline regressions for the failures observed in the first two traces."""

import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

from pi_agent_py.application.evaluation.workflow import AssertionVerifier, BudgetFeedback, ContainerFileTool
from pi_agent_py.application.evaluation.tasks import CodingTask
from pi_agent_py.application.evaluation.runner import run_task
from pi_agent_py.application.evaluation.policy import EvaluationPolicy
from pi_agent_py.domain import ContextPlan, Message, ProviderRequestContext, ShellCollectedOutput, ShellExecSpec, ShellRunResult, ToolCall, ToolExecutionContext
from pi_agent_py.domain import Finish, ModelRequestTarget, TextDelta
from pi_agent_py.testing import FakeProvider


async def invoke(tool, arguments):
    call = ToolCall("call", tool.definition.name, arguments)
    return await tool.execute(arguments, ToolExecutionContext(call, tool.definition, 1, 1))


TASK = CodingTask('demo__project-1', 'demo/project', 'a' * 40, 'Fix the defect.', 'fixture:local')


class LocalEnvironment:
    """Runs the exact container helper in a temporary local workspace, offline."""
    def __init__(self, root, *, cancel=False):
        self.root = Path(root)
        self.closed = False
        self.cancel = cancel
        self.calls = 0

    async def start(self, task):
        return "sha256:fixture"

    def resolve(self, request):
        return ShellExecSpec(request.command, str(self.root), 5)

    async def execute(self, spec):
        self.calls += 1
        if self.cancel:
            raise asyncio.CancelledError()
        argv = shlex.split(spec.command)
        self.assert_helper(argv)
        argv[0], argv[3] = sys.executable, str(self.root)
        process = await asyncio.create_subprocess_exec(*argv, cwd=self.root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        return ShellRunResult(process.returncode, None, False, False, 5,
            ShellCollectedOutput(stdout.decode()), ShellCollectedOutput(stderr.decode()))

    def assert_helper(self, argv):
        assert argv[:2] == ["python", "-c"] and argv[3] == "/testbed"

    async def export_patch(self):
        return (self.root / "bug.py").read_text() if (self.root / "bug.py").exists() else "patch"

    async def close(self):
        self.closed = True


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_remaining_budget_is_recordable_projection_and_preserves_thinking(self):
        clock = [0.0]
        feedback = BudgetFeedback(5, 100, clock=lambda: clock[0], used_requests=35)
        messages = (Message("system", "rules"), Message("user", "task"),
                    Message("assistant", "", thinking="preserved"))
        plan = ContextPlan(messages, len(messages))
        clock[0] = 91
        result = await feedback.prepare(ProviderRequestContext("r", "a", "agent_turn", 4, run_id="run", turn_id="turn"), plan)
        self.assertEqual(result.source_message_count, 3)
        self.assertEqual(result.messages[0], messages[0])
        self.assertEqual(result.messages[2:], messages[1:])
        self.assertIn('"requests_remaining_after_this": 1', result.messages[1].content)
        self.assertIn('"requests_used_before_this": 38', result.messages[1].content)
        self.assertIn('"seconds_remaining_at_preparation": 9', result.messages[1].content)
        self.assertIn("FINALIZATION", result.messages[1].content)
        self.assertEqual(plan.messages, messages)


class EditingTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_feedback_shows_modified_region_instead_of_file_header(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'bug.py'
            path.write_text('# header\n' * 200 + 'value = 1\n')
            env = LocalEnvironment(root)
            read, edit = ContainerFileTool(env, 'read'), ContainerFileTool(env, 'edit')
            version = (await invoke(read, {'path': 'bug.py', 'start_line': 200, 'end_line': 201})).structured_content['sha256']
            result = await invoke(edit, {'path': 'bug.py', 'old_text': 'value = 1', 'new_text': 'value = 2', 'expected_sha256': version})
            self.assertFalse(result.is_error)
            self.assertIn('201: value = 2', result.content)
            self.assertEqual(result.structured_content['changed_start_line'], 201)

    async def test_syntax_stale_and_ambiguous_edits_leave_original_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bug.py"
            original = b"def value():\n    return 1\n"
            path.write_bytes(original)
            env = LocalEnvironment(root)
            reader, editor = ContainerFileTool(env, "read"), ContainerFileTool(env, "edit")
            read = await invoke(reader, {"path": "bug.py"})
            self.assertIn("1:", read.content)
            version = read.structured_content["sha256"]
            args = {"path": "bug.py", "old_text": "    return 1", "new_text": "return 2", "expected_sha256": version}
            bad = await invoke(editor, args)
            self.assertTrue(bad.is_error)
            self.assertIn("SyntaxError", bad.content)
            self.assertEqual(path.read_bytes(), original)
            good = await invoke(editor, {**args, "new_text": "    return 2"})
            self.assertFalse(good.is_error)
            self.assertIn("return 2", path.read_text())
            stale = await invoke(editor, {**args, "old_text": "    return 2", "new_text": "    return 3"})
            self.assertTrue(stale.is_error)
            self.assertIn("stale", stale.content)
            version = (await invoke(reader, {"path": "bug.py"})).structured_content["sha256"]
            path.write_text("x = 1\nx = 1\n")
            version = (await invoke(reader, {"path": "bug.py"})).structured_content["sha256"]
            ambiguous = await invoke(editor, {**args, "expected_sha256": version, "old_text": "x = 1", "new_text": "x = 2"})
            self.assertTrue(ambiguous.is_error)
            self.assertEqual(path.read_text(), "x = 1\nx = 1\n")
            denied = await invoke(reader, {"path": "../outside"})
            self.assertTrue(denied.is_error)
            protected = await invoke(editor, {**args, "path": ".git/config"})
            self.assertTrue(protected.is_error)


class AssertionTests(unittest.IsolatedAsyncioTestCase):
    async def test_print_only_and_baseline_passing_scripts_cannot_verify(self):
        with tempfile.TemporaryDirectory() as root:
            env = LocalEnvironment(root)
            verifier = AssertionVerifier(env, lambda: LocalEnvironment(root), TASK, "sha256:fixture")
            no_assert = await invoke(verifier, {"script": "print('pass')"})
            self.assertTrue(no_assert.is_error)
            self.assertEqual(env.calls, 0)
            trivial = await invoke(verifier, {"script": "assert True"})
            self.assertTrue(trivial.is_error)
            self.assertFalse(verifier.snapshot("patch")["current_patch_verified"])
            skipped = await invoke(verifier, {"script": "if False:\n    assert False\n"})
            self.assertTrue(skipped.is_error)
            self.assertFalse(verifier.snapshot("patch")["current_patch_verified"])

    async def test_same_assertions_fail_base_pass_fix_and_bind_to_exact_patch(self):
        with tempfile.TemporaryDirectory() as current, tempfile.TemporaryDirectory() as base:
            (Path(base) / "bug.py").write_text("def value():\n    return 1\n")
            (Path(current) / "bug.py").write_text("def value():\n    return 2\n")
            env, baseline = LocalEnvironment(current), LocalEnvironment(base)
            verifier = AssertionVerifier(env, lambda: baseline, TASK, "sha256:fixture")
            result = await invoke(verifier, {"script": "from bug import value\nassert value() == 2\n"})
            self.assertFalse(result.is_error, result.content)
            self.assertTrue(baseline.closed)
            self.assertFalse(env.closed)
            self.assertTrue(verifier.snapshot(await env.export_patch())["current_patch_verified"])
            self.assertFalse(verifier.snapshot("changed patch")["current_patch_verified"])
            failed = await invoke(verifier, {"script": "from bug import value\nassert value() == 9\n"})
            self.assertTrue(failed.is_error)
            self.assertFalse(verifier.snapshot(await env.export_patch())["current_patch_verified"])

    async def test_cancelled_baseline_is_closed_and_no_success_is_recorded(self):
        with tempfile.TemporaryDirectory() as root:
            env, baseline = LocalEnvironment(root), LocalEnvironment(root, cancel=True)
            verifier = AssertionVerifier(env, lambda: baseline, TASK, "sha256:fixture")
            with self.assertRaises(asyncio.CancelledError):
                await invoke(verifier, {"script": "assert False"})
            self.assertTrue(baseline.closed)
            self.assertFalse(env.closed)
            self.assertFalse(verifier.snapshot("patch")["current_patch_verified"])


class WorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_runtime_records_feedback_and_red_green_tool_evidence(self):
        with tempfile.TemporaryDirectory() as current, tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as output:
            original = b"def value():\n    return 1\n"
            for root in (current, base):
                (Path(root) / "bug.py").write_bytes(original)
            provider = FakeProvider([
                [ToolCall('r', 'read', {'path': 'bug.py'}), Finish('tool_calls')],
                [ToolCall('e', 'edit', {'path': 'bug.py', 'old_text': 'return 1', 'new_text': 'return 2',
                                      'expected_sha256': hashlib.sha256(original).hexdigest()}), Finish('tool_calls')],
                [ToolCall('v', 'verify', {'script': 'from bug import value\nassert value() == 2\n'}), Finish('tool_calls')],
                [TextDelta('Verified the regression.'), Finish()],
            ])
            root = Path(output) / 'run'
            report = await run_task(TASK, LocalEnvironment(current), provider, ModelRequestTarget('fake', 'scripted'),
                root, source={'dataset': 'synthetic'}, code_commit='fixture', max_requests=4, timeout_seconds=30,
                policy=EvaluationPolicy(max_requests=4, timeout_seconds=30, min_request_interval_seconds=0),
                environment_factory=lambda: LocalEnvironment(base))
            self.assertEqual(report['status'], 'completed')
            self.assertTrue(report['verification']['current_patch_verified'])
            self.assertEqual(len(provider.requests), 4)
            self.assertIn('"requests_remaining_after_this": 0', provider.requests[-1][1].content)
            self.assertIn('"current_patch_verified": true', provider.requests[-1][1].content)
            self.assertNotIn('[Evaluation feedback', (root/'session.jsonl').read_text())
            snapshots = [json.loads(p.read_text()) for p in (root/'requests').glob('*.json')]
            self.assertTrue(all('[Evaluation feedback' in r['messages'][1]['content'] for r in snapshots))
