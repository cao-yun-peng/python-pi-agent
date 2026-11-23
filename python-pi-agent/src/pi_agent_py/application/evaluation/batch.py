"""Serial evaluation with fixed denominators, explicit graders and resume state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
import importlib.metadata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from ...domain import ModelRequestTarget
from ...ports import ModelProvider
from .cases import digest, output_lock, read_record, run_case, target_record, write_record
from .docker import CommandRunner, run_command
from .policy import EvaluationPolicy, SharedRequestLimiter
from .tasks import CodingTask, TaskEnvironment


@dataclass(frozen=True)
class GradeResult:
    status: str
    resolved: bool
    report: str
    tests_executed: bool


class CaseGrader(Protocol):
    async def evaluate(self, task: CodingTask, prediction: Path | None, output: Path,
                       *, gold: bool = False) -> GradeResult: ...


class OfficialGrader:
    """Borrow pinned local grading data; each invocation has a fresh run ID."""

    def __init__(self, dataset: Path, split: str, *, runner: CommandRunner = run_command) -> None:
        self.dataset = dataset.resolve()
        self.split = split
        self.data_sha256 = digest(self.dataset)
        rows: object = json.loads(self.dataset.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("grading data must be an array of task rows")
        self._tasks = {task.instance_id: task for row in rows
                       if isinstance(row, dict) for task in (CodingTask.from_row(row),)}
        if len(self._tasks) != len(rows):
            raise ValueError("duplicate grader task ids")
        self._runner = runner

    async def evaluate(self, task: CodingTask, prediction: Path | None, output: Path,
                       *, gold: bool = False) -> GradeResult:
        import sys
        if digest(self.dataset) != self.data_sha256:
            raise ValueError("grading dataset changed after batch selection")
        if self._tasks.get(task.instance_id) != task:
            raise ValueError("grader row and model task projection disagree")
        if not gold and prediction is None:
            return GradeResult("no_prediction", False, "", False)
        output.mkdir(parents=True, exist_ok=False)
        run_id = "eval-" + uuid.uuid4().hex
        try:
            result = await self._runner((
                sys.executable, "-m", "pi_agent_py.application.evaluation.grading",
                "--dataset_name", str(self.dataset), "--split", self.split,
                "--instance_ids", task.instance_id, "--predictions_path",
                "gold" if gold else str(prediction.resolve()) if prediction else "",
                "--run_id", run_id, "--report_dir", str(output.resolve()),
            ), 1900)
        except BaseException:
            # Terminating the host grader alone does not terminate its container.
            name = f"sweb.eval.{task.instance_id}.{run_id}".lower()
            try:
                cleanup = await self._runner(("docker", "rm", "--force", name), 30)
                if cleanup.code and "No such container" not in cleanup.stderr:
                    write_record(output / "cleanup-error.json", {"exit_code": cleanup.code})
            except Exception as error:
                write_record(output / "cleanup-error.json", {"error_type": type(error).__name__})
            raise
        (output / "process.log").write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
        if result.code:
            return GradeResult("grader_error", False, str(output), False)
        reports = list(output.glob(f"*.{run_id}.json"))
        if len(reports) != 1:
            return GradeResult("grader_error", False, str(output), False)
        report = read_record(reports[0])
        if report.get("total_instances") != 1 or report.get("submitted_ids") != [task.instance_id]:
            raise ValueError("official report does not match the selected instance")
        resolved = task.instance_id in report.get("resolved_ids", [])  # type: ignore[operator]
        executed = task.instance_id in report.get("completed_ids", [])  # type: ignore[operator]
        if resolved and not executed:
            raise ValueError("resolved report has no completed instance")
        status = "graded"
        if report.get("empty_patch_instances"):
            status = "empty_patch"
        elif report.get("error_instances") or report.get("infra_failure_instances"):
            status = "grader_error"
        return GradeResult(status, resolved, str(reports[0]), executed)


def summarize(output: Path) -> dict[str, object]:
    manifest = read_record(output / "batch.json")
    state = read_record(output / "state.json")
    ids = manifest.get("instance_ids")
    rows = state.get("rows")
    if not isinstance(ids, list) or not isinstance(rows, dict) or set(rows) != set(ids):
        raise ValueError("batch state lost selected instances")
    counts = Counter(str(row.get("execution_status", "not_started")) for row in rows.values())
    resolved = sum(row.get("resolved") is True for row in rows.values())
    totals = {key: sum(int(row.get("reported_usage", {}).get(key, 0)) for row in rows.values())
              for key in ("input_tokens", "output_tokens", "total_tokens")}
    summary: dict[str, object] = {
        "schema_version": 1, "selected_instances": len(ids), "resolved_instances": resolved,
        "resolved_fraction": resolved / len(ids) if ids else 0, "execution_counts": dict(counts),
        "grade_counts": dict(Counter(str(row.get("grade_status", "not_started")) for row in rows.values())),
        "reported_usage": totals, "rows": rows,
    }
    write_record(output / "summary.json", summary)
    return summary


async def run_batch(
    tasks: Sequence[CodingTask], environment_factory: Callable[[], TaskEnvironment],
    provider: ModelProvider, target: ModelRequestTarget, grader: CaseGrader, output: Path,
    *, policy: EvaluationPolicy, source: dict[str, str], code_commit: str,
    grading_data_sha256: str, resume: bool = False, gold_preflight: bool = True,
) -> dict[str, object]:
    ids = [task.instance_id for task in tasks]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("a batch requires a nonempty unique task list")
    versions = {}
    for name in ("pi-agent-py", "datasets", "swebench", "docker"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    identity = {"tasks": [asdict(task) for task in tasks], "target": target_record(target),
                "policy": policy.record(), "source": source, "code_commit": code_commit,
                "grading_data_sha256": grading_data_sha256, "gold_preflight": gold_preflight,
                "history": "base-only", "runtime_versions": versions}
    identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if not resume:
        output.mkdir(parents=True, exist_ok=False)
    elif not (output / "batch.json").is_file():
        raise ValueError("resume requires a batch manifest")
    with output_lock(output):
        if resume:
            manifest = read_record(output / "batch.json")
            if manifest.get("identity_hash") != identity_hash:
                raise ValueError("batch selection/configuration/code changed")
            state = read_record(output / "state.json")
        else:
            write_record(output / "batch.json", {"schema_version": 1, "identity_hash": identity_hash,
                                                 "identity": identity, "instance_ids": ids})
            state = {"rows": {key: {"instance_id": key, "execution_status": "not_started",
                                    "grade_status": "not_started", "resolved": None} for key in ids}}
            write_record(output / "state.json", state)
        rows = state.get("rows")
        if not isinstance(rows, dict) or set(rows) != set(ids):
            raise ValueError("invalid batch state")
        summarize(output)
        limiter = SharedRequestLimiter(policy.min_request_interval_seconds, initial_wait=resume)
        try:
            for task in tasks:
                row = rows[task.instance_id]
                if not isinstance(row, dict):
                    raise ValueError("invalid case state")
                if row.get("finished"):
                    continue  # Failed/empty graded cases are also final, not silently retried.
                case_root = output / "cases" / task.instance_id
                phase = "preflight"
                try:
                    if gold_preflight and not row.get("gold_passed"):
                        row["execution_status"] = "preflight"
                        write_record(output / "state.json", state)
                        summarize(output)
                        control = await grader.evaluate(task, None, output / "controls" / (task.instance_id + "-" + uuid.uuid4().hex), gold=True)
                        row["gold_report"] = control.report
                        if not control.resolved:
                            row.update(execution_status="environment_failed", grade_status="preflight_failed",
                                       resolved=False, finished=True)
                            continue
                        row["gold_passed"] = True
                        write_record(output / "state.json", state)
                        summarize(output)
                    row["execution_status"] = "running"
                    phase = "execution"
                    write_record(output / "state.json", state)
                    summarize(output)
                    execution = await run_case(
                        task, environment_factory, provider, target, case_root, policy=policy,
                        source=source, code_commit=code_commit, resume=(case_root / "case.json").exists(),
                        limiter=limiter,
                    )
                    row.update(execution_status=execution["status"], error_type=execution.get("error_type"),
                               reported_usage=execution.get("reported_usage", {}), case_path=str(case_root),
                               consumed_requests=execution.get("consumed_requests"),
                               duration_seconds=execution.get("duration_seconds"))
                    write_record(output / "state.json", state)
                    summarize(output)
                    prediction = case_root / "predictions.jsonl"
                    phase = "grading"
                    verdict = await grader.evaluate(task, prediction if prediction.is_file() else None,
                        output / "grades" / (task.instance_id + "-" + uuid.uuid4().hex))
                    row.update(grade_status=verdict.status, resolved=verdict.resolved,
                               official_report=verdict.report, tests_executed=verdict.tests_executed, finished=True)
                except asyncio.CancelledError:
                    row["execution_status"] = "interrupted"
                    if (case_root / "report.json").is_file():
                        partial = read_record(case_root / "report.json")
                        row.update(reported_usage=partial.get("reported_usage", {}),
                                   consumed_requests=partial.get("consumed_requests"),
                                   duration_seconds=partial.get("duration_seconds"))
                    raise
                except Exception as error:
                    if phase == "grading":
                        row.update(grade_status="grader_error", grade_error_type=type(error).__name__,
                                   resolved=False, finished=True)
                    else:
                        row.update(execution_status="environment_failed" if phase == "preflight" else "infrastructure_error",
                                   grade_status="not_graded", error_type=type(error).__name__, resolved=False, finished=True)
                finally:
                    write_record(output / "state.json", state)
                    summarize(output)
        finally:
            write_record(output / "state.json", state)
            summarize(output)
        return summarize(output)
