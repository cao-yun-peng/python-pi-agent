"""Immutable case manifests and bounded session/workspace continuation."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import time
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ...domain import ModelRequestTarget
from ...ports import ModelProvider
from ...storage.model_requests import FileModelRequestStore
from .policy import EvaluationPolicy, SharedRequestLimiter
from .runner import SYSTEM_PROMPT, run_task
from .tasks import CodingTask, TaskEnvironment


def read_record(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("expected a JSON object")
    return value


def write_record(path: Path, value: dict[str, object]) -> None:
    """Atomic replacement; previous manifests/reports survive failed writes."""
    pending = path.with_name(path.name + ".pending")
    with pending.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def output_lock(root: Path) -> Iterator[None]:
    """OS-owned nonblocking lock, automatically released after process death."""
    with (root / ".lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            locking = importlib.import_module("msvcrt")
            try:
                locking.locking(stream.fileno(), locking.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError("evaluation output is already in use") from error
        else:
            locking = importlib.import_module("fcntl")
            try:
                locking.flock(stream.fileno(), locking.LOCK_EX | locking.LOCK_NB)
            except OSError as error:
                raise ValueError("evaluation output is already in use") from error
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                locking.locking(stream.fileno(), locking.LK_UNLCK, 1)
            else:
                locking.flock(stream.fileno(), locking.LOCK_UN)


def target_record(target: ModelRequestTarget) -> dict[str, object]:
    return {"provider": target.provider, "model": target.model, "parameters": dict(target.parameters)}


async def run_case(
    task: CodingTask, environment_factory: Callable[[], TaskEnvironment], provider: ModelProvider,
    target: ModelRequestTarget, output: Path, *, policy: EvaluationPolicy,
    source: dict[str, str], code_commit: str, resume: bool = False,
    limiter: SharedRequestLimiter | None = None, wall_clock: Callable[[], float] = time.time,
) -> dict[str, object]:
    """Resume only verified checkpoints; never replenish time/request budgets."""
    identity = {"schema_version": 1, "task": asdict(task), "source": source,
                "target": target_record(target), "policy": policy.record(), "code_commit": code_commit,
                "history": "base-only", "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()}
    if resume:
        if not (output / "case.json").is_file():
            raise ValueError("resume requires an existing case manifest")
    else:
        output.mkdir(parents=True, exist_ok=False)
    with output_lock(output):
        if resume:
            manifest = read_record(output / "case.json")
            if manifest.get("identity") != identity:
                raise ValueError("resume task/configuration/code identity changed")
        else:
            manifest = {"identity": identity, "deadline_at": wall_clock() + policy.timeout_seconds}
            write_record(output / "case.json", manifest)
        deadline = manifest.get("deadline_at")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise ValueError("invalid case deadline")
        segments = sorted((output / "segments").glob("segment-*"))
        used_requests = 0
        previous: Path | None = None
        for segment in segments:
            records = await FileModelRequestStore(segment / "requests").read_requests()
            used_requests += len(records)
            if not (segment / "report.json").exists():
                raise ValueError("interrupted segment lacks a complete checkpoint; refusing blind replay")
            previous = segment
        if resume and previous is not None:
            last_report = read_record(previous / "report.json")
            checkpoint = read_record(previous / "checkpoint.json")
            if set(checkpoint) != {"session.jsonl", "patch.diff"}:
                raise ValueError("invalid checkpoint manifest")
            for name, expected in checkpoint.items():
                if digest(previous / name) != expected:
                    raise ValueError("checkpoint changed since export")
            if last_report.get("status") == "completed":
                return read_record(output / "report.json")
        remaining_requests = policy.max_requests - used_requests
        remaining_seconds = deadline - wall_clock()
        if remaining_requests <= 0 or remaining_seconds <= 0:
            result: dict[str, object] = read_record(output / "report.json") if (output / "report.json").is_file() else {}
            result.update({
                "status": "budget_exhausted", "resolved": None, "instance_id": task.instance_id,
                "consumed_requests": used_requests, "segments": len(segments),
                "budget_remaining_seconds": max(0, remaining_seconds),
                "duration_seconds": max(0, wall_clock() - (deadline - policy.timeout_seconds)),
            })
            write_record(output / "report.json", result)
            return result
        current = output / "segments" / f"segment-{len(segments) + 1:04d}"
        shared = limiter or SharedRequestLimiter(policy.min_request_interval_seconds, initial_wait=resume)
        try:
            segment_report = await run_task(
                task, environment_factory(), provider, target, current, source=source,
                code_commit=code_commit, max_requests=remaining_requests, timeout_seconds=remaining_seconds,
                policy=policy, limiter=shared, resume_from=previous,
                expected_image_id=(str(last_report["image_id"]) if previous is not None else None),
                environment_factory=environment_factory,
            )
        finally:
            if (current / "report.json").exists():
                segment_report = read_record(current / "report.json")
                result = dict(segment_report)
                result["instance_id"] = task.instance_id
                result["segments"] = len(segments) + 1
                result["segment_duration_seconds"] = result.get("duration_seconds")
                result["duration_seconds"] = max(0, wall_clock() - (deadline - policy.timeout_seconds))
                result["latest_segment"] = str(current.relative_to(output))
                result["consumed_requests"] = used_requests + len(
                    await FileModelRequestStore(current / "requests").read_requests())
                result["budget_remaining_seconds"] = max(0, deadline - wall_clock())
                totals: dict[str, int] = {key: 0 for key in ("input_tokens", "output_tokens", "total_tokens")}
                for segment in [*segments, current]:
                    usage = read_record(segment / "report.json").get("reported_usage", {})
                    if isinstance(usage, dict):
                        for key in totals:
                            if type(value := usage.get(key)) is int:
                                totals[key] += value
                result["reported_usage"] = totals
                wait_seconds = 0.0
                for segment in [*segments, current]:
                    value = read_record(segment / 'report.json').get('rate_limit_wait_seconds', 0)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        wait_seconds += value
                result['rate_limit_wait_seconds'] = wait_seconds
                for name in ("patch.diff", "predictions.jsonl"):
                    if (current / name).is_file():
                        (output / name).write_bytes((current / name).read_bytes())
                if not (current / "patch.diff").is_file():
                    (output / "predictions.jsonl").unlink(missing_ok=True)
                    result["prediction_segment"] = None
                else:
                    result["prediction_segment"] = str(current.relative_to(output))
                if (current / "patch.diff").is_file() and not (current / "predictions.jsonl").is_file():
                    prediction = {"instance_id": task.instance_id, "model_name_or_path": target.model,
                                  "model_patch": (current / "patch.diff").read_text(encoding="utf-8")}
                    (output / "predictions.jsonl").write_text(json.dumps(prediction) + "\n", encoding="utf-8")
                write_record(output / "report.json", result)
        return result
