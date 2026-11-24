"""Opt-in PG/Redis benchmark. No provider configuration or paid model calls."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from hashlib import sha256
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Protocol, cast
from uuid import uuid4

from ..domain import JSONValue
from ..errors import SessionValidationError
from ..ports import SessionLeaseManager
from ..runtime.lease_heartbeat import LeaseHeartbeatSettings
from ..storage._postgres_driver import PostgresConnection, PostgresPool
from ..storage.postgres import PostgresSessionStore
from ..storage.redis_lease import (
    RedisLeaseClient,
    RedisLeaseManager,
    RedisLeaseSettings,
)
from .benchmark_report import BenchmarkSettings, SCENARIOS, Scenario
from .recovery_benchmark import (
    BenchmarkExecutionStore,
    BenchmarkPeer,
    BenchmarkTrace,
    run_benchmark,
)


class _Pool(PostgresPool, Protocol):
    async def close(self) -> None: ...


class _Admin(PostgresConnection, Protocol):
    async def close(self) -> None: ...


class _Redis(RedisLeaseClient, Protocol):
    async def aclose(self) -> None: ...
    async def info(self, section: str) -> dict[str, object]: ...


def source_metadata() -> dict[str, JSONValue]:
    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=Path(__file__).parent,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    digest = sha256()
    for name in ("benchmark_report.py", "recovery_benchmark.py", "benchmark_cli.py"):
        digest.update(name.encode())
        digest.update(
            Path(__file__).with_name(name).read_bytes().replace(b"\r\n", b"\n")
        )
    status = git(
        "status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=all"
    )
    return {
        "code_commit": git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(status) if status is not None else None,
        "benchmark_source_sha256": digest.hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "machine": platform.machine(),
        "asyncpg": version("asyncpg"),
        "redis_py": version("redis"),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }


async def benchmark(settings: BenchmarkSettings) -> dict[str, JSONValue]:
    dsn, url = (
        os.environ.get("PI_AGENT_POSTGRES_TEST_DSN"),
        os.environ.get("PI_AGENT_REDIS_TEST_URL"),
    )
    if not dsn or not url:
        raise ValueError("explicit PG/Redis test endpoints required")
    driver = importlib.import_module("asyncpg")
    redis = importlib.import_module("redis.asyncio")
    retry = importlib.import_module("redis.asyncio.retry")
    backoff = importlib.import_module("redis.backoff")
    metadata = source_metadata()
    schema = "pi_bench_" + uuid4().hex
    prefix = "pi:benchmark:" + uuid4().hex + ":"
    trace = BenchmarkTrace()
    peers: list[BenchmarkPeer] = []
    async with AsyncExitStack() as stack:
        admin = cast(_Admin, await driver.connect(dsn, timeout=10, command_timeout=10))
        stack.push_async_callback(admin.close)
        # This identifier is generated here; no caller-provided schema can be dropped.
        await admin.execute(f'CREATE SCHEMA "{schema}"')

        async def drop_schema() -> None:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')

        stack.push_async_callback(drop_schema)
        row = await admin.fetchrow(
            "SELECT current_setting('server_version') AS version"
        )
        metadata["postgres_server"] = str(row["version"]) if row else None
        for _ in range(settings.workers):
            pool = cast(
                _Pool,
                await driver.create_pool(
                    dsn,
                    min_size=1,
                    max_size=settings.pool_size,
                    timeout=10,
                    command_timeout=30,
                    server_settings={"search_path": schema, "application_name": schema},
                ),
            )

            async def close_pool(pool: _Pool = pool) -> None:
                await asyncio.wait_for(pool.close(), 10)

            stack.push_async_callback(close_pool)
            client = cast(
                _Redis,
                redis.from_url(
                    url,
                    socket_timeout=1,
                    socket_connect_timeout=1,
                    retry=retry.Retry(backoff.NoBackoff(), 0),
                ),
            )
            stack.push_async_callback(client.aclose)
            server = await client.info("server")
            metadata["redis_server"] = str(server.get("redis_version", "unknown"))
            leases: SessionLeaseManager = RedisLeaseManager(
                client, settings=RedisLeaseSettings(key_prefix=prefix)
            )
            store = BenchmarkExecutionStore(pool, trace)
            peers.append(BenchmarkPeer(PostgresSessionStore(pool), store, leases))
        # Each invocation owns an empty schema; migration is explicit at this entry point.
        await cast(BenchmarkExecutionStore, peers[0].store).ensure_schema()
        metadata["database_scope"] = (
            "new isolated schema per invocation, removed after all Workers/pools close"
        )
        return await run_benchmark(tuple(peers), trace, settings, metadata)


def write_report(path: Path, report: dict[str, JSONValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--pool-size", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--scan-limit", type=int, default=32)
    parser.add_argument("--poll-interval", type=float, default=0.025)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--ttl", type=float, default=30)
    parser.add_argument("--heartbeat-interval", type=float, default=10)
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    parser.add_argument(
        "--output", type=Path, default=Path(".tmp/recovery-benchmark.json")
    )
    args = parser.parse_args(argv)
    try:
        settings = BenchmarkSettings(
            samples=args.samples,
            warmup=args.warmup,
            workers=args.workers,
            pool_size=args.pool_size,
            concurrency=args.concurrency,
            scan_limit=args.scan_limit,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
            scenarios=cast(tuple[Scenario, ...], tuple(args.scenario or SCENARIOS)),
            heartbeat=LeaseHeartbeatSettings(
                ttl_seconds=args.ttl, heartbeat_interval=args.heartbeat_interval
            ),
        )
    except (ValueError, SessionValidationError) as error:
        parser.error(str(error))  # Settings contain no secrets or endpoints.
    try:
        report = asyncio.run(benchmark(settings))
    except KeyboardInterrupt:
        report = {
            "schema_version": 1,
            "status": "cancelled",
            "error_type": "KeyboardInterrupt",
        }
    except Exception as error:
        # Driver exceptions can contain passwords/DSNs; never print exception text.
        report = {
            "schema_version": 1,
            "status": "aborted",
            "error_type": type(error).__name__,
        }
    report.setdefault("settings", settings.data())
    report.setdefault("provider", "FakeProvider")
    report.setdefault("paid_model_calls", 0)
    write_report(args.output, report)
    print(
        json.dumps(
            {"status": report["status"], "report": str(args.output)}, ensure_ascii=False
        )
    )
    return 0 if report["status"] == "valid" else 1


if __name__ == "__main__":
    sys.exit(main())
