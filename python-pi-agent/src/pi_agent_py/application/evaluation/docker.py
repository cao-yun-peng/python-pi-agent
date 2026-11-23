"""Persistent, unmounted Docker task environment implementing ShellExecutor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol
from uuid import uuid4
from typing import Literal

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
    async def __call__(self, argv: tuple[str, ...], timeout: float, *, stdin: bytes | None = None) -> CommandResult: ...


async def run_command(argv: tuple[str, ...], timeout: float, *, stdin: bytes | None = None) -> CommandResult:
    """Bound memory while draining both streams; kill and reap on interruption."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
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
                if stdin is not None:
                    assert process.stdin is not None
                    try:
                        process.stdin.write(stdin)
                        await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # The command's exit code remains the result.
                    finally:
                        process.stdin.close()
                await process.wait()
        stdout, out_cut = out.result()
        stderr, err_cut = err.result()
        return CommandResult(process.returncode or 0, stdout, stderr, out_cut or err_cut)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise


_BASE_HISTORY = r'''
import pathlib, shutil, subprocess, tempfile
root = pathlib.Path('/testbed')
original = root / '.git'
if not original.is_dir() or original.is_symlink():
    raise RuntimeError('expected an owned Git directory')
with tempfile.TemporaryDirectory(prefix='pi-eval-history-') as temporary:
    clone = pathlib.Path(temporary) / 'base'
    subprocess.run(['git', 'clone', '--no-local', '--no-checkout', '--single-branch',
                    '--no-tags', '--depth', '1', 'file:///testbed', str(clone)], check=True)
    shutil.rmtree(original)
    shutil.move(str(clone / '.git'), str(original))
    subprocess.run(['git', '-C', str(root), 'remote', 'remove', 'origin'], check=True)
    subprocess.run(['git', '-C', str(root), 'reset', '--mixed', 'HEAD'], check=True)
    count = subprocess.check_output(['git', '-C', str(root), 'rev-list', '--all', '--count']).strip()
    if count != b'1':
        raise RuntimeError('task history was not restricted')
'''


class DockerTaskEnvironment:
    """No host mounts, sockets, credentials or network; writable task container.

    This is an evaluation environment, NOT the full workspace sandbox contract.
    Images must already exist locally. Timeout invalidates the whole container.
    """

    def __init__(self, *, runner: CommandRunner = run_command,
                 history: Literal["base-only", "full"] = "base-only") -> None:
        if history not in {"base-only", "full"}:
            raise ValueError("unsupported task history policy")
        self.history = history
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
            if self.history == "base-only":
                await self._checked(("docker", "exec", "-w", "/testbed", self._name,
                                     "python3", "-c", _BASE_HISTORY))
            self._ready = True
            self._base_commit = task.base_commit
            return image_id
        except BaseException:
            await self.close()
            raise

    def resolve(self, request: ShellExecRequest) -> ShellExecSpec:
        path = PurePosixPath(request.workdir or ".")
        if ".." in path.parts or "\\" in str(path):
            raise ValueError("workdir must stay inside /testbed")
        if path.is_absolute():
            if not path.is_relative_to('/testbed'):
                raise ValueError("workdir must stay inside /testbed")
            path = path.relative_to('/testbed')
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

    async def restore_patch(self, patch: str) -> None:
        if not self._ready:
            raise SandboxUnavailableError("task environment is not ready")
        if not patch.strip():
            return
        result = await self._runner(("docker", "exec", "-i", "-w", "/testbed", self._name,
                                    "git", "apply", "--binary", "-"), 120, stdin=patch.encode("utf-8"))
        if result.code:
            raise SandboxUnavailableError("workspace checkpoint did not apply")

    async def close(self) -> None:
        self._ready = False
        if self._owned:
            result = await self._runner(("docker", "rm", "--force", self._name), 30)
            if result.code and "No such container" not in result.stderr:
                raise SandboxUnavailableError("evaluation container cleanup failed")
            self._owned = False
