"""Evaluation-only request feedback and tools over the borrowed task container."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import replace
import hashlib
import json
import shlex
import time
from typing import Literal, cast

from ...domain import ContextPlan, JSONObject, JSONValue, Message, ProviderRequestContext, ShellExecRequest, ShellRunResult, ToolDefinition, ToolExecutionContext, ToolOutput
from ...ports import ShellExecutor
from .tasks import CodingTask, TaskEnvironment


_FILES = r'''
import hashlib, json, os, pathlib, stat, sys, tempfile
root = pathlib.Path(sys.argv[1]).resolve()
args = json.loads(sys.argv[2])
try:
    relative = pathlib.PurePosixPath(args['path'])
    if relative.is_absolute() or '..' in relative.parts or '\\' in args['path']:
        raise ValueError('path must be relative and inside the workspace')
    if args['operation'] == 'edit' and '.git' in relative.parts:
        raise ValueError('editing Git metadata is not permitted')
    path = (root / str(relative)).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('path must resolve to a workspace file')
    if path.stat().st_size > 1000000:
        raise ValueError('file exceeds the 1 MB limit')
    original = path.read_bytes()
    text = original.decode('utf-8')
    version = hashlib.sha256(original).hexdigest()
    if args['operation'] == 'edit':
        if version != args['expected_sha256']:
            raise ValueError('stale file version; read the file again')
        old, new = args['old_text'], args['new_text']
        if not old or len(old) > 65536 or len(new) > 65536 or text.count(old) != 1:
            raise ValueError('old_text must match exactly once; replacement limit is 64 KB')
        updated = text.replace(old, new, 1)
        if path.suffix == '.py':
            try:
                compile(updated, str(path), 'exec')
            except SyntaxError as error:
                raise ValueError('SyntaxError: ' + str(error)) from error
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pi-edit-', delete=False) as handle:
                temporary = pathlib.Path(handle.name)
                handle.write(updated.encode('utf-8'))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        text = updated
        version = hashlib.sha256(path.read_bytes()).hexdigest()
    start, end = args.get('start_line', 1), args.get('end_line', 160)
    if type(start) is not int or type(end) is not int or start < 1 or end < start or end-start >= 500:
        raise ValueError('read range must contain 1 to 500 lines')
    lines = text.splitlines()
    excerpt = '\n'.join(str(i+1) + ': ' + lines[i] for i in range(start-1, min(end, len(lines))))
    print(json.dumps({'path':str(relative), 'sha256':version, 'total_lines':len(lines),
                      'content':excerpt[:20000], 'truncated':len(excerpt)>20000,
                      'syntax_checked':args['operation']=='edit' and path.suffix=='.py'}))
except Exception as error:
    print(json.dumps({'error':type(error).__name__, 'details':str(error)}))
    sys.exit(2)
'''

_ASSERTIONS = r'''
import ast, json, os, sys, traceback
os.chdir(sys.argv[1])
source = json.loads(sys.argv[2])['script']
seen = set()
metadata = {'passed':False, 'assertions_executed':0, 'error_type':None, 'script_completed':False}
try:
    tree = ast.parse(source, filename='regression_assertions.py')
    lines = {node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)}
    if not lines:
        raise ValueError('at least one Python assert is required')
    def trace(frame, event, arg):
        if event == 'line' and frame.f_code.co_filename == 'regression_assertions.py' and frame.f_lineno in lines:
            seen.add(frame.f_lineno)
        return trace
    sys.settrace(trace)
    exec(compile(tree, 'regression_assertions.py', 'exec'), {'__name__':'__main__'})
    metadata['script_completed'] = True
    if not seen:
        raise ValueError('no assertion was executed')
    metadata['passed'] = True
except BaseException as error:
    metadata['error_type'] = type(error).__name__
    traceback.print_exc()
finally:
    sys.settrace(None)
    metadata['assertions_executed'] = len(seen)
    print('\n_PI_ASSERTION_RESULT_' + json.dumps(metadata), flush=True)
sys.exit(0 if metadata['passed'] else 1)
'''


async def _execute(executor: ShellExecutor, helper: str, payload: JSONObject) -> ShellRunResult:
    command = 'python -c ' + shlex.quote(helper) + ' /testbed ' + shlex.quote(json.dumps(payload))
    return await executor.execute(executor.resolve(ShellExecRequest(command, timeout_seconds=120)))


class ContainerFileTool:
    """Container-only exact edit, with optimistic version and pre-write syntax checks."""

    def __init__(self, executor: ShellExecutor, operation: Literal['read', 'edit']) -> None:
        self._executor, self._operation = executor, operation
        properties: dict[str, JSONValue] = {'path': {'type': 'string'}}
        required: list[JSONValue] = ['path']
        if operation == 'read':
            properties.update(start_line={'type': 'integer'}, end_line={'type': 'integer'})
            description = 'Read a UTF-8 container file with line numbers and its SHA-256 version. Paths are relative to /testbed; default lines 1-160, maximum 500 lines.'
        else:
            properties.update(old_text={'type': 'string'}, new_text={'type': 'string'}, expected_sha256={'type': 'string'})
            required += ['old_text', 'new_text', 'expected_sha256']
            description = 'Replace exactly one old_text region in a container file. First read its SHA-256. Stale, ambiguous, or syntactically invalid Python edits are rejected without changing the file. Use this instead of line-index or sed edits.'
        self._definition = ToolDefinition(operation, description,
            {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
            side_effects='read' if operation == 'read' else 'write', requires_approval=True,
            retry_safe=operation == 'read', replay_safe=operation == 'read', timeout_seconds=90)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        del context
        result = await _execute(self._executor, _FILES, {**arguments, 'operation': self._operation})
        try:
            record = json.loads(result.stdout.text)
            if not isinstance(record, dict):
                raise ValueError('expected file result')
        except (ValueError, TypeError):
            return ToolOutput('Container helper returned invalid or truncated output.', is_error=True)
        return ToolOutput(json.dumps(record, ensure_ascii=False), cast(JSONValue, record),
                          is_error=result.exit_code != 0 or result.stdout.truncated)


class AssertionVerifier:
    """Run identical self-contained assertions on original and candidate containers.

    Borrow the candidate. Own/close each clean control, also on cancellation.
    This is regression evidence, not a replacement for official hidden grading.
    """

    def __init__(self, environment: TaskEnvironment, factory: Callable[[], TaskEnvironment] | None,
                 task: CodingTask, image_id: str) -> None:
        self._environment, self._factory, self._task, self._image_id = environment, factory, task, image_id
        self._verified: str | None = None
        self._records: list[JSONValue] = []
        self._status = 'missing'
        self._definition = ToolDefinition('verify',
            'Execute a self-contained Python regression script containing assert statements. The SAME script must fail on a fresh original-base container and pass on your current patch, with assertions actually executed. Imports must refer to production code, not newly created helper files. Include meaningful expected values and relevant configuration/edge cases; print-only checks are refused. Does not expose official hidden tests.',
            {'type': 'object', 'properties': {'script': {'type': 'string'}}, 'required': ['script'], 'additionalProperties': False},
            side_effects='write', requires_approval=True, retry_safe=False, replay_safe=False, timeout_seconds=300)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def snapshot(self, patch: str) -> dict[str, JSONValue]:
        digest = hashlib.sha256(patch.encode()).hexdigest()
        return {'status': self._status, 'current_patch_verified': bool(patch.strip()) and self._verified == digest,
                'verified_patch_sha256': self._verified, 'current_patch_sha256': digest,
                'attempts': list(self._records)}

    async def progress(self) -> dict[str, JSONValue]:
        verified = False
        if self._verified is not None:
            verified = self.snapshot(await self._environment.export_patch())['current_patch_verified'] is True
        return {'status': self._status, 'current_patch_verified': verified}

    async def execute(self, arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        del context
        self._verified, self._status = None, 'failed'
        source = arguments.get('script')
        if not isinstance(source, str) or not source.strip() or len(source) > 65536:
            return ToolOutput('Provide a self-contained assertion script of at most 64 KB.', is_error=True)
        try:
            has_assert = any(isinstance(node, ast.Assert) for node in ast.walk(ast.parse(source)))
        except SyntaxError:
            has_assert = False
        if not has_assert:
            return ToolOutput('A valid Python assert statement is required; print-only checks are not verification.', is_error=True)
        if self._factory is None:
            return ToolOutput('A clean baseline environment factory was not configured.', is_error=True)
        baseline = self._factory()
        try:
            image = await baseline.start(self._task)
            if image != self._image_id:
                return ToolOutput('Baseline image identity differs from the candidate.', is_error=True)
            before = await _execute(baseline, _ASSERTIONS, {'script': source})
        finally:
            await baseline.close()
        after = await _execute(self._environment, _ASSERTIONS, {'script': source})
        def metadata(result: ShellRunResult) -> dict[str, JSONValue]:
            lines = [line.removeprefix('_PI_ASSERTION_RESULT_') for line in result.stdout.text.splitlines()
                     if line.startswith('_PI_ASSERTION_RESULT_')]
            try:
                record = json.loads(lines[-1])
                return cast(dict[str, JSONValue], record) if isinstance(record, dict) else {}
            except (IndexError, ValueError):
                return {}
        original, candidate = metadata(before), metadata(after)
        patch = await self._environment.export_patch()
        valid = (before.exit_code == 1 and original.get('script_completed') is False
                 and original.get('error_type') in {'AssertionError', 'TypeError', 'AttributeError', 'ValueError'}
                 and after.exit_code == 0 and candidate.get('passed') is True
                 and isinstance(candidate.get('assertions_executed'), int) and cast(int, candidate['assertions_executed']) > 0
                 and bool(patch.strip()) and not any(result.stdout.truncated or result.stderr.truncated for result in (before, after)))
        record: dict[str, JSONValue] = {'script_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'baseline': original, 'candidate': candidate, 'baseline_exit_code': before.exit_code,
            'candidate_exit_code': after.exit_code, 'accepted': valid,
            'patch_sha256': hashlib.sha256(patch.encode()).hexdigest()}
        self._records.append(record)
        if valid:
            self._verified, self._status = str(record['patch_sha256']), 'red_green'
        content = json.dumps(record) + '\nBASELINE:\n' + (before.stdout.text + before.stderr.text)[-12000:] + '\nCANDIDATE:\n' + (after.stdout.text + after.stderr.text)[-12000:]
        if not valid:
            content += '\nNot verified: the same assertions must detect the original defect and pass on the current nonempty patch. Fix the code or strengthen your regression assertions.'
        return ToolOutput(content, record, is_error=not valid)


class BudgetFeedback:
    """Request-only system feedback; final inputs are persisted by RecordingModelProvider."""

    def __init__(self, max_requests: int, timeout_seconds: float, *,
                 clock: Callable[[], float] = time.monotonic, used_requests: int = 0,
                 deadline: float | None = None, verifier: AssertionVerifier | None = None) -> None:
        self._maximum, self._used, self._clock = max_requests, used_requests, clock
        self._deadline = deadline if deadline is not None else clock() + timeout_seconds
        self._verifier = verifier

    async def prepare(self, context: ProviderRequestContext, plan: ContextPlan) -> ContextPlan:
        status: JSONValue = 'missing' if self._verifier is None else await self._verifier.progress()
        remaining = max(0, int(self._deadline - self._clock()))
        after = max(0, self._maximum - context.request_number)
        record = {'requests_used_before_this': self._used + context.request_number - 1,
                  'requests_remaining_after_this': after, 'seconds_remaining_at_preparation': remaining,
                  'verification_status': status}
        instruction = 'Batch independent reads in one bash call. Use read/edit for source changes. Run verify with meaningful assertions before finishing; existing tests alone may miss the reported defect.'
        if after <= 4 or remaining <= 150:
            instruction = 'FINALIZATION: reserve remaining requests for verify and a final answer. Stop exploration and Git history searches. Do not discard a working patch as cleanup. ' + instruction
        reminder = Message('system', '[Evaluation feedback at request preparation; wall time includes rate-limit waits]\n' + json.dumps(record) + '\n' + instruction)
        messages = plan.messages
        offset = 1 if messages and messages[0].role == 'system' else 0
        return replace(plan, messages=messages[:offset] + (reminder,) + messages[offset:])
