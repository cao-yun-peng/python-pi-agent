"""Model-facing Bash consumer over the provider-neutral ShellExecutor port."""

from __future__ import annotations

import asyncio
from math import isfinite
from pathlib import PurePosixPath

from ..domain import (
    JSONObject,
    JSONValue,
    ShellCollectedOutput,
    ShellExecRequest,
    ShellExecSpec,
    ShellRunResult,
    ShellSandboxInfo,
    SandboxProcessRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    ToolSideEffects,
    WorkspaceAccessMode,
)
from ..errors import SandboxTimeoutError, SandboxUnavailableError
from ..ports import ProcessSandbox, ShellExecutor


class SandboxedBashExecutor:
    """Maps one-shot Bash semantics onto an injected process sandbox."""

    def __init__(
        self,
        sandbox: ProcessSandbox,
        *,
        mode: WorkspaceAccessMode = "workspace-write",
        default_timeout_seconds: float = 120.0,
        max_timeout_seconds: float = 600.0,
        require_full_enforcement: bool = True,
    ) -> None:
        if mode not in ("read-only", "workspace-write"):
            raise ValueError("unknown workspace access mode")
        _validate_positive_finite_timeout(
            default_timeout_seconds,
            "default_timeout_seconds",
        )
        _validate_positive_finite_timeout(
            max_timeout_seconds,
            "max_timeout_seconds",
        )
        if default_timeout_seconds > max_timeout_seconds:
            raise ValueError(
                "default_timeout_seconds must not exceed max_timeout_seconds"
            )
        if type(require_full_enforcement) is not bool:
            raise TypeError("require_full_enforcement must be a boolean")

        self._sandbox = sandbox
        self._mode = mode
        self._default_timeout_seconds = default_timeout_seconds
        self._max_timeout_seconds = max_timeout_seconds
        self._require_full_enforcement = require_full_enforcement

    def resolve(self, request: ShellExecRequest) -> ShellExecSpec:
        """Resolve model-facing options to a bounded container execution spec."""
        timeout_seconds = request.timeout_seconds
        if timeout_seconds is None:
            timeout_seconds = self._default_timeout_seconds
        timeout_seconds = min(timeout_seconds, self._max_timeout_seconds)

        relative_workdir = request.workdir or "."
        if "\\" in relative_workdir:
            raise ValueError("shell workdir must use POSIX path separators")
        path = PurePosixPath(relative_workdir)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("shell workdir must stay inside the workspace")
        workdir = "/workspace"
        if path.parts:
            workdir = f"/workspace/{path.as_posix()}"

        return ShellExecSpec(
            command=request.command,
            workdir=workdir,
            timeout_seconds=timeout_seconds,
        )

    async def execute(self, spec: ShellExecSpec) -> ShellRunResult:
        """Run Bash through the sandbox and preserve orthogonal result facts."""
        request = SandboxProcessRequest(
            argv=("/bin/bash", "-lc", spec.command),
            workdir=spec.workdir,
            mode=self._mode,
            timeout_seconds=spec.timeout_seconds,
        )
        try:
            result = await self._sandbox.run(request)
        except SandboxTimeoutError:
            return ShellRunResult(
                exit_code=None,
                signal=None,
                timed_out=True,
                cancelled=False,
                timeout_seconds=spec.timeout_seconds,
                stdout=ShellCollectedOutput(""),
                stderr=ShellCollectedOutput(""),
                sandbox=ShellSandboxInfo(
                    mode=self._mode,
                    enforcement=None,
                ),
            )

        if self._require_full_enforcement and result.enforcement != "full":
            raise SandboxUnavailableError(
                "process sandbox reported partial enforcement; refusing Bash run"
            )

        return ShellRunResult(
            exit_code=result.exit_code,
            signal=None,
            timed_out=False,
            cancelled=False,
            timeout_seconds=spec.timeout_seconds,
            stdout=ShellCollectedOutput(
                result.stdout,
                truncated=result.stdout_truncated,
            ),
            stderr=ShellCollectedOutput(
                result.stderr,
                truncated=result.stderr_truncated,
            ),
            sandbox=ShellSandboxInfo(
                mode=self._mode,
                enforcement=result.enforcement,
            ),
        )


def _validate_positive_finite_timeout(value: object, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be positive and finite")


class BashTool:
    """Runs a fresh Bash command through an injected ShellExecutor."""

    def __init__(
        self,
        executor: ShellExecutor,
        *,
        requires_approval: bool = True,
        side_effects: ToolSideEffects = "write",
        name: str = "bash",
    ) -> None:
        self._executor = executor
        self._definition = ToolDefinition(
            name=name,
            description=(
                "Run one Bash command in the configured workspace. Each call "
                "uses a fresh shell; use workdir instead of relying on a prior cd."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The Bash command to execute.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Working directory resolved by the executor inside "
                            "the configured workspace."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": (
                            "Requested timeout; the executor applies its "
                            "configured default and upper bound."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            side_effects=side_effects,
            requires_approval=requires_approval,
            retry_safe=False,
            replay_safe=False,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del context
        command = arguments["command"]
        if not isinstance(command, str):
            raise TypeError("validated Bash command must be a string")

        workdir_value = arguments.get("workdir")
        if workdir_value is not None and not isinstance(workdir_value, str):
            raise TypeError("validated Bash workdir must be a string")

        timeout_value = arguments.get("timeout_seconds")
        if timeout_value is not None and (
            not isinstance(timeout_value, (int, float))
            or isinstance(timeout_value, bool)
        ):
            raise TypeError("validated Bash timeout_seconds must be a number")

        request = ShellExecRequest(
            command=command,
            workdir=workdir_value,
            timeout_seconds=timeout_value,
        )
        spec = self._executor.resolve(request)
        result = await self._executor.execute(spec)
        if result.cancelled:
            raise asyncio.CancelledError("Bash execution was cancelled")

        return ToolOutput(
            content=_render_result(result),
            structured_content=_structured_result(result, workdir=spec.workdir),
            # A nonzero process exit is an observed command result. Providers
            # raise for infrastructure failures before a ToolOutput exists.
            is_error=False,
        )


def _render_stream(label: str, output: ShellCollectedOutput) -> str:
    text = output.text
    if not output.truncated:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    recovery = (
        f"; full output: {output.spill_path}"
        if output.spill_path is not None
        else ""
    )
    return f"{text}[{label} truncated{recovery}]"


def _render_result(result: ShellRunResult) -> str:
    stdout = _render_stream("stdout", result.stdout)
    stderr = _render_stream("stderr", result.stderr)
    body = stdout
    if stderr:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"[stderr]\n{stderr}"
    if not body:
        body = "(no output)"

    markers: list[str] = []
    sandbox = result.sandbox
    if sandbox is not None:
        if sandbox.runner_failed:
            markers.append(
                "[sandbox: runner failed before the command could execute]"
            )
        elif sandbox.denied:
            markers.append(
                f"[sandbox: file access denied under {sandbox.mode} mode]"
            )
    if result.timed_out:
        markers.append(f"[timed out after {result.timeout_seconds:g}s]")
    if result.signal is not None:
        markers.append(f"[killed by signal: {result.signal}]")
    elif result.exit_code is None:
        markers.append("[exit code: null]")
    elif result.exit_code != 0:
        markers.append(f"[exit code: {result.exit_code}]")

    if not markers:
        return body
    separator = "" if body.endswith("\n") else "\n"
    rendered_markers = "\n".join(markers)
    return f"{body}{separator}{rendered_markers}"


def _collected_value(output: ShellCollectedOutput) -> dict[str, JSONValue]:
    value: dict[str, JSONValue] = {
        "text": output.text,
        "truncated": output.truncated,
    }
    if output.spill_path is not None:
        value["spill_path"] = output.spill_path
    return value


def _sandbox_value(info: ShellSandboxInfo) -> dict[str, JSONValue]:
    return {
        "mode": info.mode,
        "enforcement": info.enforcement,
        "denied": info.denied,
        "runner_failed": info.runner_failed,
    }


def _structured_result(
    result: ShellRunResult,
    *,
    workdir: str,
) -> dict[str, JSONValue]:
    value: dict[str, JSONValue] = {
        "exit_code": result.exit_code,
        "signal": result.signal,
        "timed_out": result.timed_out,
        "cancelled": result.cancelled,
        "timeout_seconds": result.timeout_seconds,
        "workdir": workdir,
        "stdout": _collected_value(result.stdout),
        "stderr": _collected_value(result.stderr),
    }
    if result.sandbox is not None:
        value["sandbox"] = _sandbox_value(result.sandbox)
    return value
