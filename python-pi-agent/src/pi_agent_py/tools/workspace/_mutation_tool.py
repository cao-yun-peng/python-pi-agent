"""Shared model projection for the two mutation tools; no scheduling policy."""

from dataclasses import asdict
from typing import cast

from ...domain import JSONObject, ToolOutput, WorkspaceMutationResult
from ...errors import WorkspaceFileError


def mutation_output(result: WorkspaceMutationResult) -> ToolOutput:
    return ToolOutput(f"{'Created' if result.created else 'Updated'} {result.path}\n{result.diff}", cast(JSONObject, asdict(result)))


def mutation_error(error: WorkspaceFileError) -> ToolOutput:
    unknown = error.code in ("outcome_unknown", "unavailable", "protocol_error")
    return ToolOutput(
        "Workspace mutation outcome is unknown; do not retry automatically." if unknown else str(error),
        {"code": "outcome_unknown" if unknown else error.code}, is_error=True,
    )


def text_argument(arguments: JSONObject, name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ValueError("text argument required")
    return value


def expected_version(arguments: JSONObject) -> str | None:
    value = arguments.get("expected_version")
    if value is not None and not isinstance(value, str):
        raise ValueError("expected_version must be text")
    return value
