"""Persistent, unmounted Docker task environment implementing ShellExecutor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol
from uuid import uuid4

from ...domain import ShellCollectedOutput, ShellExecRequest, ShellExecSpec, ShellRunResult, ShellSandboxInfo
from ...errors import SandboxUnavailableError
from .tasks import CodingTask


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False


class CommandRunner(Protocol):
    async def __call__(self, argv: tuple[str, ...], timeout: float) -> CommandResult: ...


async def run_command(argv: tuple[str, ...], timeout: float) -> CommandResult:
    """Bound memory while draining both streams; kill and reap on interruption."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise SandboxUnavailableError("evaluation command could not start") from error

    async def read(stream: asyncio.StreamReader | None) -> tuple[str, bool]:
        assert stream is not None
        data = bytearray()
        truncated = False
        while block := await stream.read(65536):
            remaining = max(0, 5_000_000 - len(data))
            data.extend(block[:remaining])
            truncated |= len(block) > remaining
        return data.decode("utf-8", errors="replace"), truncated

    try:
        async with asyncio.timeout(timeout):
            async with asyncio.TaskGroup() as group:
                out = group.create_task(read(process.stdout))
                err = group.create_task(read(process.stderr))
                await process.wait()
        stdout, out_cut = out.result()
        stderr, err_cut = err.result()
        return CommandResult(process.returncode or 0, stdout, stderr, out_cut or err_cut)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise


class DockerTaskEnvironment:
    """No host mounts, sockets, credentials or network; writable task container.

    This is an evaluation environment, NOT the full workspace sandbox contract.
    Images must already exist locally. Timeout invalidates the whole container.
    """

    def __init__(self, *, runner: CommandRunner = run_command) -> None:
        self._runner = runner
        self._name = "pi-eval-" + uuid4().hex
        self._owned = False
        self._ready = False
        self._base_commit = ""

    async def _checked(self, argv: tuple[str, ...], timeout: float = 120) -> CommandResult:
        result = await self._runner(argv, timeout)
        if result.code:
            raise SandboxUnavailableError("Docker evaluation operation failed; check local image/daemon")
        return result

    async def start(self, task: CodingTask) -> str:
        if self._owned:
            raise RuntimeError("task environment is single-use")
        image = await self._checked(("docker", "image", "inspect", "--format", "{{.Id}}", task.image))
        image_id = image.stdout.strip()
        if not image_id.startswith("sha256:"):
            raise SandboxUnavailableError("Docker did not return an immutable image identity")
        # Own the name before creation so failed/cancelled starts are also cleaned.
        self._owned = True
        try:
            await self._checked((
                "docker", "create", "--name", self._name, "--pull", "never",
                "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", "4g", "--cpus", "2", "--pids-limit", "512",
                "--workdir", "/testbed", "--entrypoint", "/bin/sleep", image_id, "infinity",
            ))
            await self._checked(("docker", "start", self._name))
            await self._checked(("docker", "exec", "-w", "/testbed", self._name,
                                 "git", "reset", "--hard", task.base_commit))
            await self._checked(("docker", "exec", "-w", "/testbed", self._name,
                                 "git", "clean", "-fd"))
            self._ready = True
            self._base_commit = task.base_commit
            return image_id
        except BaseException:
            await self.close()
            raise

    def resolve(self, request: ShellExecRequest) -> ShellExecSpec:
        path = PurePosixPath(request.workdir or ".")
        if path.is_absolute() or ".." in path.parts or "\\" in str(path):
            raise ValueError("workdir must be relative to /testbed")
        return ShellExecSpec(request.command, str(PurePosixPath("/testbed") / path),
                             min(request.timeout_seconds or 120, 600))

    async def execute(self, spec: ShellExecSpec) -> ShellRunResult:
        if not self._ready:
            raise SandboxUnavailableError("task environment is not ready")
        path = PurePosixPath(spec.workdir)
        if not path.is_relative_to("/testbed") or ".." in path.parts:
            raise ValueError("workdir must stay inside /testbed")
        try:
            result = await self._runner((
                "docker", "exec", "-w", spec.workdir, self._name, "/bin/bash", "-lc",
                "source /opt/miniconda3/bin/activate && conda activate testbed &&\n" + spec.command,
            ), spec.timeout_seconds)
        except BaseException:
            # Killing docker exec alone leaves descendants running in the container.
            await self.close()
            raise
        return ShellRunResult(
            result.code, None, False, False, spec.timeout_seconds,
            ShellCollectedOutput(result.stdout, result.truncated),
            ShellCollectedOutput(result.stderr, result.truncated),
            ShellSandboxInfo("workspace-write", enforcement="partial"),
        )

    async def export_patch(self) -> str:
        if not self._ready:
            raise SandboxUnavailableError("cannot export an invalidated task environment")
        result = await self._checked((
            "docker", "exec", "-w", "/testbed", self._name, "/bin/bash", "-lc",
            f"git add -N -- . && git diff --binary {self._base_commit}",
        ))
        if result.truncated:
            raise SandboxUnavailableError("patch exceeded capture budget; refusing partial prediction")
        return result.stdout

    async def close(self) -> None:
        self._ready = False
        if self._owned:
            result = await self._runner(("docker", "rm", "--force", self._name), 30)
            if result.code and "No such container" not in result.stderr:
                raise SandboxUnavailableError("evaluation container cleanup failed")
            self._owned = False
