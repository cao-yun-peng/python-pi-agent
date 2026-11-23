"""Offline workflow assembly with scripted task ports, not a benchmark score."""

import argparse
import asyncio
import json
from pathlib import Path
import shlex

from pi_agent_py.application.evaluation.policy import EvaluationPolicy
from pi_agent_py.application.evaluation.runner import run_task
from pi_agent_py.application.evaluation.tasks import CodingTask
from pi_agent_py.domain import Finish, ModelRequestTarget, ShellCollectedOutput, ShellExecSpec, ShellRunResult, TextDelta, ToolCall
from pi_agent_py.testing import FakeProvider


class ScriptedWorkspace:
    """Only supplies fixtures; production tools and Runtime handle orchestration."""
    def __init__(self):
        self.changed = False

    async def start(self, task):
        return 'synthetic-workflow-image'

    def resolve(self, request):
        return ShellExecSpec(request.command, '/testbed', 1)

    async def execute(self, spec):
        args = json.loads(shlex.split(spec.command)[-1])
        if 'script' in args:
            record = {'passed':self.changed, 'script_completed':self.changed,
                      'assertions_executed':1, 'error_type':None if self.changed else 'AssertionError'}
            text = '_PI_ASSERTION_RESULT_' + json.dumps(record)
            code = 0 if self.changed else 1
        else:
            self.changed = args['operation'] == 'edit' or self.changed
            text = json.dumps({'sha256':'a'*64, 'content':'1: value = 1', 'syntax_checked':self.changed})
            code = 0
        return ShellRunResult(code, None, False, False, 1, ShellCollectedOutput(text), ShellCollectedOutput(''))

    async def export_patch(self):
        return 'synthetic changed workspace' if self.changed else ''

    async def close(self):
        pass


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    provider = FakeProvider([
        [ToolCall('read', 'read', {'path':'example.py'}), Finish('tool_calls')],
        [ToolCall('edit', 'edit', {'path':'example.py', 'old_text':'value = 1', 'new_text':'value = 2', 'expected_sha256':'a'*64}), Finish('tool_calls')],
        [ToolCall('verify', 'verify', {'script':'assert value == 2'}), Finish('tool_calls')],
        [TextDelta('Synthetic regression verified.'), Finish()],
    ])
    report = await run_task(CodingTask('demo__workflow-1', 'demo/project', 'a'*40, 'Synthetic task', 'fixture:offline'),
        ScriptedWorkspace(), provider, ModelRequestTarget('fake', 'scripted'), args.output,
        source={'dataset':'synthetic', 'revision':'v1', 'split':'demo'}, code_commit='example',
        max_requests=6, timeout_seconds=30,
        policy=EvaluationPolicy(max_requests=6, timeout_seconds=30, min_request_interval_seconds=0),
        environment_factory=ScriptedWorkspace)
    print(json.dumps({'synthetic_only':True, 'status':report['status'],
                      'verification':report.get('verification'), 'output':str(args.output)}))


asyncio.run(main())
