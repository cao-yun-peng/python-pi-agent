"""Model-facing Bash consumer over the provider-neutral ShellExecutor port."""

from __future__ import annotations

import asyncio

from .domain import (
    JSONObject,
    JSONValue,
    ShellCollectedOutput,
    ShellExecRequest,
    ShellRunResult,
    ShellSandboxInfo,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from .ports import ShellExecutor


class BashTool:
    """Runs a fresh Bash command through an injected ShellExecutor."""

    def __init__(
        self,
        executor: ShellExecutor,
        *,
        requires_approval: bool = True,
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
            side_effects="write",
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
