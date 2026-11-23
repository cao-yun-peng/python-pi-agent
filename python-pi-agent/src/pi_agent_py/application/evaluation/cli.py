"""SWE-bench Lite task preparation, inference, and reference grading commands."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from ...domain import Finish, ModelRequestTarget, TextDelta, ToolCall
from ...testing import FakeProvider
from ..configuration import load_app_config
from ..provider_catalog import default_provider_catalog
from .docker import DockerTaskEnvironment
from .runner import run_task
from .tasks import CodingTask
from .policy import EvaluationPolicy
from .cases import read_record, digest, run_case, output_lock
from .batch import OfficialGrader, run_batch, summarize


DATASET = "SWE-bench/SWE-bench_Lite"


def prepare(revision: str, split: str, ids: list[str], output: Path) -> None:
    """Explicit network step. Gold stays in grader-data.json outside containers."""
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("revision must be a pinned HF commit hash")
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("select unique instance ids")
    try:
        load_dataset = importlib.import_module("datasets").load_dataset
    except ImportError as error:
        raise RuntimeError("prepare requires the optional datasets package") from error
    dataset = load_dataset(DATASET, split=split, revision=revision)
    rows = [dict(row) for row in dataset if row["instance_id"] in ids]
    if {row["instance_id"] for row in rows} != set(ids):
        raise ValueError("selected ids were not found in this split/revision")
    tasks = [asdict(CodingTask.from_row(row)) for row in rows]
    output.mkdir(parents=True, exist_ok=False)
    source = {"dataset": DATASET, "revision": revision, "split": split}
    (output / "tasks.json").write_text(json.dumps({"source": source, "tasks": tasks}, indent=2), encoding="utf-8")
    (output / "grader-data.json").write_text(json.dumps(rows), encoding="utf-8")


async def infer(args: argparse.Namespace) -> int:
    manifest = read_record(args.tasks)
    raw_tasks, raw_source = manifest.get("tasks"), manifest.get("source")
    if not isinstance(raw_tasks, list) or not isinstance(raw_source, dict):
        raise ValueError("invalid prepared task manifest")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_source.items()):
        raise ValueError("manifest source values must be strings")
    tasks = [CodingTask.from_row(row) for row in raw_tasks if isinstance(row, dict)]
    if len(tasks) != len(raw_tasks) or len({task.instance_id for task in tasks}) != len(tasks):
        raise ValueError("invalid or duplicate task rows")
    if args.command == "run":
        tasks = [task for task in tasks if task.instance_id == args.instance_id]
        if len(tasks) != 1:
            raise ValueError("select exactly one task from the prepared manifest")
    values = read_record(args.policy) if args.policy else {}
    if args.max_requests is not None:
        values["max_requests"] = args.max_requests
    if args.timeout is not None:
        values["timeout_seconds"] = args.timeout
    policy = EvaluationPolicy.parse(values)
    catalog = default_provider_catalog()
    loaded = load_app_config(args.config, env_path=args.env, provider_catalog=catalog)
    if loaded.spec.capabilities:
        raise ValueError("benchmark provider config must not enable application capabilities")
    if loaded.spec.resilience.enabled:
        raise ValueError("evaluation retries are configured in --policy, not provider YAML resilience")
    commit = subprocess.run(("git", "rev-parse", "HEAD"), check=True, capture_output=True, text=True).stdout.strip()
    source = {str(key): str(value) for key, value in raw_source.items()}
    source["tasks_sha256"] = digest(args.tasks)
    source["harness_dirty"] = str(bool(subprocess.run(
        ("git", "status", "--porcelain", "--ignore-submodules=all"),
        check=True, capture_output=True, text=True,
    ).stdout.strip())).lower()
    source["provider_timeout_seconds"] = str(getattr(loaded.provider_config, "timeout_seconds", "default"))
    source["provider_endpoint_sha256"] = hashlib.sha256(str(getattr(loaded.provider_config, "base_url", "")).encode()).hexdigest()
    settings = {name: getattr(loaded.provider_config, name) for name in (
        "temperature", "max_tokens", "thinking", "thinking_keep", "reasoning_effort", "tool_stream",
    ) if isinstance(getattr(loaded.provider_config, name, None), (str, int, float, bool))}
    built = await catalog.build(loaded.provider_name, loaded.provider_config)
    try:
        target = ModelRequestTarget(loaded.provider_name, loaded.spec.provider.model, settings)
        if args.command == "batch":
            grader = OfficialGrader(args.grader_data, source.get("split", "dev"))
            report = await run_batch(tasks, DockerTaskEnvironment, built.provider, target, grader, args.output,
                policy=policy, source=source, code_commit=commit, grading_data_sha256=grader.data_sha256,
                resume=args.resume)
            print(json.dumps({key: report[key] for key in ("selected_instances", "resolved_instances", "execution_counts", "grade_counts")}))
            return 0
        report = await run_case(tasks[0], DockerTaskEnvironment, built.provider, target, args.output,
                               source=source, code_commit=commit, policy=policy, resume=args.resume)
        print(json.dumps({"status": report["status"], "resolved": None, "output": str(args.output)}))
        return 0 if report["status"] == "completed" else 1
    finally:
        close = getattr(built.provider, "aclose", None)
        if close is not None:
            await close()


async def demo(output: Path) -> None:
    """Exercise production Runtime and persistence without Docker or network."""
    from ...domain import ShellCollectedOutput, ShellExecRequest, ShellExecSpec, ShellRunResult

    class DemoEnvironment:
        async def start(self, task: CodingTask) -> str:
            return "synthetic-offline-environment"

        def resolve(self, request: ShellExecRequest) -> ShellExecSpec:
            return ShellExecSpec("synthetic fix", "/testbed", 1)

        async def execute(self, spec: ShellExecSpec) -> ShellRunResult:
            return ShellRunResult(0, None, False, False, 1, ShellCollectedOutput("test passed"), ShellCollectedOutput(""))

        async def export_patch(self) -> str:
            return "diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-bug\n+fix\n"

        async def close(self) -> None:
            pass

    task = CodingTask("demo__project-1", "demo/project", "a" * 40, "Fix the synthetic example.", "demo:offline")
    provider = FakeProvider([[ToolCall("fix", "bash", {"command": "synthetic fix"}), Finish("tool_calls")],
                             [TextDelta("Synthetic fix complete."), Finish()]])
    report = await run_task(task, DemoEnvironment(), provider, ModelRequestTarget("fake", "scripted"),
                            output, source={"dataset": "synthetic", "revision": "v1", "split": "demo"},
                            code_commit="demo")
    print(json.dumps({"status": report["status"], "resolved": None, "output": str(output)}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    p.add_argument("--revision", required=True)
    p.add_argument("--split", choices=("dev", "test"), default="dev")
    p.add_argument("--instance-id", action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("run")
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--instance-id", required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--env", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--max-requests", type=int)
    p.add_argument("--timeout", type=float)
    p.add_argument("--policy", type=Path)
    p.add_argument("--resume", action="store_true")
    p = commands.add_parser("batch", help="Serial cases, shared admission, gold controls and official grading")
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--grader-data", type=Path, required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--env", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--max-requests", type=int)
    p.add_argument("--timeout", type=float)
    p.add_argument("--policy", type=Path)
    p.add_argument("--resume", action="store_true")
    p = commands.add_parser("summarize", help="Rebuild summary from saved state, without model calls")
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("demo")
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("grade")
    p.add_argument("--dataset", type=Path, required=True, help="Pinned grader-data.json")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--predictions", type=Path)
    mode.add_argument("--gold", action="store_true", help="Environment control using reference patches")
    p.add_argument("--instance-id", action="append", required=True)
    p.add_argument("--split", choices=("dev", "test"), default="dev")
    p.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.revision, args.split, args.instance_id, args.output)
        return 0
    if args.command in {"run", "batch"}:
        return asyncio.run(infer(args))
    if args.command == "summarize":
        with output_lock(args.output):
            print(json.dumps(summarize(args.output), ensure_ascii=False, indent=2))
        return 0
    if args.command == "demo":
        asyncio.run(demo(args.output))
        return 0
    return subprocess.run((
        sys.executable, "-m", "pi_agent_py.application.evaluation.grading",
        "--dataset_name", str(args.dataset.resolve()), "--split", args.split,
        "--predictions_path", "gold" if args.gold else str(args.predictions.resolve()),
        "--instance_ids", *args.instance_id, "--max_workers", "1", "--run_id", args.run_id,
    ), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
