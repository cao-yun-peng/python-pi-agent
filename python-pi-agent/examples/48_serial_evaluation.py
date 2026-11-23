"""Offline batch using production orchestration and scripted dependencies."""

import argparse
import asyncio
import json
from pathlib import Path

from pi_agent_py.application.evaluation.batch import GradeResult, run_batch
from pi_agent_py.application.evaluation.policy import EvaluationPolicy
from pi_agent_py.application.evaluation.tasks import CodingTask
from pi_agent_py.domain import Finish, ModelRequestTarget, ShellExecRequest, ShellExecSpec, ShellRunResult
from pi_agent_py.testing import FakeProvider


class Workspace:
    async def start(self, task: CodingTask) -> str:
        return "synthetic-environment"

    async def restore_patch(self, patch: str) -> None:
        pass

    def resolve(self, request: ShellExecRequest) -> ShellExecSpec:
        return ShellExecSpec(request.command, "/testbed", 1)

    async def execute(self, spec: ShellExecSpec) -> ShellRunResult:
        raise AssertionError("this script does not request tools")

    async def export_patch(self) -> str:
        return "synthetic patch"

    async def close(self) -> None:
        pass


class Grader:
    async def evaluate(self, task: CodingTask, prediction: Path | None, output: Path,
                       *, gold: bool = False) -> GradeResult:
        return GradeResult("synthetic", True, "", True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tasks = [CodingTask(f"demo__project-{number}", "demo/project", "a" * 40,
                        "Synthetic exercise", "demo:offline") for number in (1, 2)]
    result = await run_batch(tasks, Workspace, FakeProvider([[Finish()], [Finish()]]),
        ModelRequestTarget("fake", "scripted"), Grader(), args.output,
        policy=EvaluationPolicy(min_request_interval_seconds=0),
        source={"dataset": "synthetic", "revision": "v1", "split": "demo"},
        code_commit="example", grading_data_sha256="synthetic")
    print(json.dumps({"synthetic_only": True, "selected_instances": result["selected_instances"],
                      "resolved_instances": result["resolved_instances"]}))


if __name__ == "__main__":
    asyncio.run(main())
