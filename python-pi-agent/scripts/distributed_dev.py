"""Cross-platform local PG/Redis environment. Only explicit commands start services."""

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
COMPOSE = PACKAGE / "dev" / "distributed" / "compose.yaml"
INTEGRATION_MODULES = (
    "test_postgres_session_store",
    "test_postgres_execution",
    "test_redis_lease",
    "test_redis_pg_heartbeat",
    "test_postgres_runtime",
    "test_postgres_recovery",
    "test_recovery_benchmark_integration",
)


@dataclass(frozen=True)
class Environment:
    project: str = "pi-agent-dev"
    pg_port: int = 55432
    redis_port: int = 56379

    def __post_init__(self) -> None:
        if not re.fullmatch(r"pi-agent-[a-z0-9][a-z0-9-]*", self.project):
            raise ValueError(
                "project must start with pi-agent- and contain lowercase letters/digits/hyphens"
            )
        for port in (self.pg_port, self.redis_port):
            if type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError("ports must be integers between 1024 and 65535")
        if self.pg_port == self.redis_port:
            raise ValueError("PG and Redis host ports must differ")

    def variables(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            PI_AGENT_DEV_PG_PORT=str(self.pg_port),
            PI_AGENT_DEV_REDIS_PORT=str(self.redis_port),
            PI_AGENT_POSTGRES_DSN=f"postgresql://postgres@127.0.0.1:{self.pg_port}/pi_agent_dev",
            PI_AGENT_REDIS_URL=f"redis://127.0.0.1:{self.redis_port}/0",
        )
        environment["PI_AGENT_POSTGRES_TEST_DSN"] = environment["PI_AGENT_POSTGRES_DSN"]
        environment["PI_AGENT_REDIS_TEST_URL"] = environment["PI_AGENT_REDIS_URL"]
        # Explicit opt-in only; never inherit a request to restart some other Redis.
        environment.pop("PI_AGENT_REDIS_TEST_RESTART_CONTAINER", None)
        environment["PYTHONPATH"] = (
            str(PACKAGE / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        )
        return environment

    def compose(self, *arguments: str) -> list[str]:
        return ["docker", "compose", "-f", str(COMPOSE), "-p", self.project, *arguments]


def invoke(
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    cwd: Path = PACKAGE,
    capture: bool = False,
) -> str | None:
    # Argument vector throughout; never interpolate credentials into shell text.
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not capture:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    result.check_returncode()
    return result.stdout.strip() if capture else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "up",
            "down",
            "status",
            "migrate",
            "test",
            "demo",
            "benchmark",
            "verify",
        ),
    )
    parser.add_argument(
        "--project", default=os.environ.get("PI_AGENT_DEV_PROJECT", "pi-agent-dev")
    )
    parser.add_argument(
        "--pg-port", type=int, default=os.environ.get("PI_AGENT_DEV_PG_PORT", "55432")
    )
    parser.add_argument(
        "--redis-port",
        type=int,
        default=os.environ.get("PI_AGENT_DEV_REDIS_PORT", "56379"),
    )
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument(
        "--output", type=Path, default=PACKAGE / ".tmp" / "recovery-benchmark.json"
    )
    parser.add_argument("--restart-redis", action="store_true")
    args = parser.parse_args(argv)
    try:
        selected = Environment(args.project, args.pg_port, args.redis_port)
        if args.samples <= 0:
            raise ValueError("samples must be positive")
    except ValueError as error:
        parser.error(str(error))
    environment = selected.variables()

    def action(name: str) -> None:
        if name == "up":
            invoke(
                selected.compose("up", "-d", "--wait", "--wait-timeout", "60"),
                environment,
            )
        elif name == "down":
            # Persist PG volume by default. No broad prune or unrelated containers.
            invoke(selected.compose("down", "--timeout", "10"), environment)
        elif name == "status":
            invoke(selected.compose("ps"), environment)
        elif name == "migrate":
            invoke(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "pi_agent_py.storage.postgres_cli",
                    "migrate",
                ],
                environment,
            )
        elif name == "test":
            if args.restart_redis:
                container = invoke(
                    selected.compose("ps", "-q", "redis"), environment, capture=True
                )
                if not container or not re.fullmatch(r"[0-9a-f]{12,64}", container):
                    raise ValueError("exactly one running development Redis required")
                environment["PI_AGENT_REDIS_TEST_RESTART_CONTAINER"] = container
            try:
                invoke(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "unittest",
                        *INTEGRATION_MODULES,
                        "-v",
                    ],
                    environment,
                    cwd=PACKAGE / "tests",
                )
            finally:
                environment.pop("PI_AGENT_REDIS_TEST_RESTART_CONTAINER", None)
        elif name == "demo":
            for flags in (("--migrate",), ("--partial-stream",)):
                invoke(
                    [sys.executable, "-B", "examples/56_recovery_workers.py", *flags],
                    environment,
                )
        elif name == "benchmark":
            invoke(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "pi_agent_py.application.benchmark_cli",
                    "--samples",
                    str(args.samples),
                    "--output",
                    str(args.output.resolve()),
                ],
                environment,
            )

    try:
        for name in (
            ("up", "migrate", "test", "demo", "benchmark")
            if args.command == "verify"
            else (args.command,)
        ):
            action(name)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print("distributed_dev_failed=" + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
