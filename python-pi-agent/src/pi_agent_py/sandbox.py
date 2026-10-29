"""Process sandbox providers with explicit, fail-closed isolation semantics."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .domain import SandboxProcessRequest, SandboxProcessResult
from .errors import (
    SandboxTimeoutError,
    SandboxUnavailableError,
)


class _AsyncReader(Protocol):
    async def read(self, count: int = -1) -> bytes: ...


class _RunningProcess(Protocol):
    returncode: int | None
    stdout: _AsyncReader | None
    stderr: _AsyncReader | None

    async def communicate(self) -> tuple[bytes, bytes]: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class _ProcessLauncher(Protocol):
    def __call__(
        self,
        *args: str,
        stdout: int | None = None,
        stderr: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Awaitable[_RunningProcess]: ...


_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DockerProcessSandbox:
    """Runs exact argv in a locked-down Docker workspace container.

    The harness stays on the host. Only the configured workspace is mounted,
    the container root is read-only, Linux capabilities are dropped, and
    network access is disabled unless the application explicitly enables it.
    Docker must already be installed and the configured image must already be
    present; the provider never silently falls back to a host process.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        image: str,
        docker_executable: str = "docker",
        environment: Mapping[str, str] | None = None,
        network_enabled: bool = False,
        memory_limit: str = "512m",
        cpu_limit: float = 1.0,
        process_limit: int = 128,
        tmpfs_size: str = "64m",
        max_output_bytes: int = 1_000_000,
        launcher: _ProcessLauncher | None = None,
    ) -> None:
        root = Path(workspace_root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        if "," in str(root):
            raise ValueError("workspace_root must not contain a comma")
        for label, value in (
            ("image", image),
            ("docker_executable", docker_executable),
            ("memory_limit", memory_limit),
            ("tmpfs_size", tmpfs_size),
        ):
            if not value.strip():
                raise ValueError(f"{label} must not be empty")
        if cpu_limit <= 0:
            raise ValueError("cpu_limit must be greater than zero")
        if process_limit <= 0:
            raise ValueError("process_limit must be greater than zero")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be greater than zero")

        copied_environment = dict(environment or {})
        for name, value in copied_environment.items():
            if not _ENVIRONMENT_NAME.fullmatch(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(
                    "environment values must be strings without NUL"
                )

        self._workspace_root = root
        self._image = image
        self._docker_executable = docker_executable
        self._environment = copied_environment
        self._network_enabled = network_enabled
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._process_limit = process_limit
        self._tmpfs_size = tmpfs_size
        self._max_output_bytes = max_output_bytes
        self._launcher = launcher or asyncio.create_subprocess_exec

    async def run(self, request: SandboxProcessRequest) -> SandboxProcessResult:
        container_name = f"pi-agent-{uuid4().hex}"
        arguments = self.build_command(request, container_name=container_name)
        try:
            process = await self._launcher(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self._environment},
            )
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise SandboxUnavailableError(
                "Docker could not be started; refusing to run on the host"
            ) from error

        try:
            if request.timeout_seconds is None:
                stdout, stderr, stdout_truncated, stderr_truncated = (
                    await _capture_output(
                        process,
                        self._max_output_bytes,
                    )
                )
            else:
                async with asyncio.timeout(request.timeout_seconds):
                    stdout, stderr, stdout_truncated, stderr_truncated = (
                        await _capture_output(
                            process,
                            self._max_output_bytes,
                        )
                    )
        except TimeoutError as error:
            await self._stop_container(container_name, process)
            raise SandboxTimeoutError(
                "sandboxed command exceeded its timeout"
            ) from error
        except asyncio.CancelledError:
            await self._stop_container(container_name, process)
            raise

        returncode = process.returncode
        if returncode is None:
            returncode = await process.wait()
        if returncode == 125:
            raise SandboxUnavailableError(
                "Docker rejected the sandbox configuration or is unavailable"
            )

        return SandboxProcessResult(
            exit_code=returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            enforcement="full",
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def build_command(
        self,
        request: SandboxProcessRequest,
        *,
        container_name: str,
    ) -> tuple[str, ...]:
        """Build deterministic Docker argv for inspection and unit tests."""
        if not container_name.strip() or "\x00" in container_name:
            raise ValueError("container_name must be a non-empty safe string")
        mount = (
            f"type=bind,source={self._workspace_root},target=/workspace"
        )
        if request.mode == "read-only":
            mount += ",readonly"

        arguments = [
            self._docker_executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--pull",
            "never",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--pids-limit",
            str(self._process_limit),
            "--memory",
            self._memory_limit,
            "--cpus",
            str(self._cpu_limit),
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self._tmpfs_size}",
            "--mount",
            mount,
            "--workdir",
            request.workdir,
            "--network",
            "bridge" if self._network_enabled else "none",
        ]
        for name in sorted(self._environment):
            arguments.extend(("--env", name))
        arguments.append(self._image)
        arguments.extend(request.argv)
        return tuple(arguments)

    async def _stop_container(
        self,
        container_name: str,
        process: _RunningProcess,
    ) -> None:
        try:
            cleanup = await self._launcher(
                self._docker_executable,
                "rm",
                "--force",
                container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(5):
                await cleanup.communicate()
        except (FileNotFoundError, PermissionError, OSError, TimeoutError):
            pass
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def _capture_output(
    process: _RunningProcess,
    limit: int,
) -> tuple[bytes, bytes, bool, bool]:
    if process.stdout is None or process.stderr is None:
        raise SandboxUnavailableError("sandbox process pipes are unavailable")
    queue: asyncio.Queue[tuple[str, bytes | None]] = asyncio.Queue(maxsize=4)

    async def pump(label: str, reader: _AsyncReader) -> None:
        while True:
            chunk = await reader.read(65_536)
            if not chunk:
                await queue.put((label, None))
                return
            await queue.put((label, chunk))

    pumps = (
        asyncio.create_task(pump("stdout", process.stdout)),
        asyncio.create_task(pump("stderr", process.stderr)),
    )
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    completed = 0
    try:
        while completed < 2:
            label, chunk = await queue.get()
            if chunk is None:
                completed += 1
                continue
            output = outputs[label]
            output.extend(chunk)
            if len(output) > limit:
                del output[: len(output) - limit]
                truncated[label] = True
    finally:
        for task in pumps:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
    return (
        bytes(outputs["stdout"]),
        bytes(outputs["stderr"]),
        truncated["stdout"],
        truncated["stderr"],
    )
