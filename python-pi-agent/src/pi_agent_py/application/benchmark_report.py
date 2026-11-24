"""Pure benchmark settings and statistics; no infrastructure or model I/O."""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil, isfinite
from typing import Literal

from ..domain import JSONValue
from ..runtime.lease_heartbeat import LeaseHeartbeatSettings
from ..runtime.recovery import RecoveryWorkerSettings

Scenario = Literal["text", "partial_stream", "final_assistant", "tool_tail"]
SCENARIOS: tuple[Scenario, ...] = (
    "text",
    "partial_stream",
    "final_assistant",
    "tool_tail",
)
DATASET_VERSION = "recovery-fake-v1"


@dataclass(frozen=True, slots=True)
class BenchmarkSettings:
    samples: int = 20  # Per scenario, excluding warmup.
    warmup: int = 2
    workers: int = 2
    pool_size: int = 4  # Per independent Worker pool.
    concurrency: int = 2  # Per Worker.
    scan_limit: int = 32
    poll_interval: float = 0.025
    timeout: float = 120.0  # Per batch; real wall clock only in explicit benchmark.
    scenarios: tuple[Scenario, ...] = SCENARIOS
    heartbeat: LeaseHeartbeatSettings = LeaseHeartbeatSettings()

    def __post_init__(self) -> None:
        for name in ("samples", "workers", "pool_size", "concurrency", "scan_limit"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.warmup) is not int or self.warmup < 0:
            raise ValueError("warmup must be a non-negative integer")
        for value in (self.poll_interval, self.timeout):
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError("benchmark deadlines must be finite and positive")
        if not self.scenarios or len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("scenarios must be a nonempty unique selection")
        if any(scenario not in SCENARIOS for scenario in self.scenarios):
            raise ValueError("unknown benchmark scenario")
        # Validate the exact scheduling budgets used by the runner before opening I/O.
        RecoveryWorkerSettings(
            scan_limit=self.scan_limit,
            max_concurrent_sessions=self.concurrency,
            poll_interval=self.poll_interval,
        )
        if 5 >= self.heartbeat.ttl_seconds - self.heartbeat.safety_margin:
            raise ValueError("benchmark claim deadline must fit the coordination TTL")

    def data(self) -> dict[str, JSONValue]:
        hb = self.heartbeat
        return {
            "samples_per_scenario": self.samples,
            "warmup_text_samples": self.warmup,
            "workers": self.workers,
            "pool_min_per_worker": 1,
            "pool_max_per_worker": self.pool_size,
            "concurrency_per_worker": self.concurrency,
            "scan_limit": self.scan_limit,
            "poll_interval_seconds": self.poll_interval,
            "batch_timeout_seconds": self.timeout,
            "scenarios": list(self.scenarios),
            "ttl_seconds": hb.ttl_seconds,
            "heartbeat_interval_seconds": hb.heartbeat_interval,
            "redis_deadline_seconds": hb.redis_timeout,
            "pg_deadline_seconds": hb.grant_timeout,
            "safety_margin_seconds": hb.safety_margin,
            "retry_delay_seconds": 0.5,
            "max_retry_delay_seconds": 0.5,
            "max_attempts": 3,
            "max_requests": 4,
            "max_dispatches": 6,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    session_id: str
    run_id: str
    scenario: Scenario
    state: str
    error_code: str | None
    attempt: int
    epoch: int
    dispatches: int
    model_requests: int
    tool_executions: int
    prompt_count: int
    unknown_results: int
    event_count: int
    correctness: bool
    completion_ms: float | None
    recovery_ms: float | None
    owners: tuple[str, ...]

    def data(self) -> dict[str, JSONValue]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "scenario": self.scenario,
            "state": self.state,
            "error_code": self.error_code,
            "attempt": self.attempt,
            "epoch": self.epoch,
            "dispatches": self.dispatches,
            "model_requests": self.model_requests,
            "tool_executions": self.tool_executions,
            "prompt_count": self.prompt_count,
            "unknown_results": self.unknown_results,
            "event_count": self.event_count,
            "correctness": self.correctness,
            "completion_ms": self.completion_ms,
            "recovery_ms": self.recovery_ms,
            "owners": list(self.owners),
        }


def distribution(values: Sequence[float]) -> dict[str, JSONValue]:
    """Nearest-rank percentiles: keep sample size and estimator visible."""
    if any(not isfinite(value) or value < 0 for value in values):
        raise ValueError("latencies must be finite and non-negative")
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "estimator": "nearest_rank",
        **{
            f"p{percent}_ms": ordered[ceil(percent / 100 * len(ordered)) - 1]
            if ordered
            else None
            for percent in (50, 95, 99)
        },
        "min_ms": ordered[0] if ordered else None,
        "max_ms": ordered[-1] if ordered else None,
    }


def summarize(
    scenario: Scenario,
    samples: Sequence[BenchmarkSample],
    elapsed: float,
    attempts: Mapping[str, int],
    *,
    timed_out: bool,
) -> dict[str, JSONValue]:
    if not isfinite(elapsed) or elapsed <= 0:
        raise ValueError("measurement duration must be positive")
    completed = sum(sample.state == "completed" for sample in samples)
    # The denominator includes acquisition conflicts as well as claimed attempts.
    conflicts = attempts.get("busy", 0)
    acquisitions = attempts.get("claimed", 0) + conflicts
    failures = Counter(
        sample.error_code or sample.state
        for sample in samples
        if sample.state != "completed"
    )
    return {
        "scenario": scenario,
        "sample_count": len(samples),
        "completed": completed,
        "correct": sum(sample.correctness for sample in samples),
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "completed_runs_per_second": completed / elapsed,
        "completion_latency": distribution(
            [s.completion_ms for s in samples if s.completion_ms is not None]
        ),
        "recovery_latency": distribution(
            [s.recovery_ms for s in samples if s.recovery_ms is not None]
        ),
        "conflicts": conflicts,
        "acquisition_attempts": acquisitions,
        "conflict_rate": conflicts / acquisitions if acquisitions else None,
        "attempt_events": dict(attempts),
        "terminal_failure_categories": dict(failures),
        "cross_worker_recoveries": sum(len(set(s.owners)) > 1 for s in samples),
        "durable_event_count": sum(s.event_count for s in samples),
        "samples": [sample.data() for sample in samples],
    }
