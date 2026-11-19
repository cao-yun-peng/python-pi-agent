"""Shared read-only model projection, with explicit operation-specific schemas."""

from dataclasses import asdict
from typing import Literal, cast

from ...domain import JSONObject, JSONValue, ToolDefinition, ToolExecutionContext, ToolOutput, WorkspaceQueryRequest
from ...errors import WorkspaceFileError
from ...ports import WorkspaceQueries


class QueryTool:
    def __init__(self, files: WorkspaceQueries, operation: Literal["ls", "find", "grep"], description: str,
                 *, requires_approval: bool = False) -> None:
        self._files, self._operation = files, operation
        properties: JSONObject = {"path": {"type": "string", "default": ".", "description": "Workspace-relative directory; grep also accepts a file."}}
        if operation != "ls":
            properties["pattern"] = {"type": "string"}
        self._definition = ToolDefinition(operation, description,
            {"type": "object", "properties": properties, "required": [] if operation == "ls" else ["pattern"], "additionalProperties": False},
            side_effects="read", requires_approval=requires_approval, retry_safe=True, replay_safe=False, timeout_seconds=35)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        del context
        try:
            path, pattern = arguments.get("path", "."), arguments.get("pattern")
            if not isinstance(path, str) or (pattern is not None and not isinstance(pattern, str)):
                raise ValueError("invalid query arguments")
            request = WorkspaceQueryRequest(self._operation, path, pattern)
        except ValueError:
            return ToolOutput("Invalid workspace query: use a relative path and a bounded nonempty pattern where required.", is_error=True)
        try:
            result = await self._files.query(request)
        except WorkspaceFileError as error:
            return ToolOutput(str(error), {"code": error.code}, is_error=True)
        metadata: JSONObject = {"entries": [cast(JSONValue, asdict(e)) for e in result.entries],
            "matches": [cast(JSONValue, asdict(m)) for m in result.matches], "truncated": result.truncated,
            "scanned_entries": result.scanned_entries, "skipped_files": result.skipped_files}
        rows = [e.path + ("/" if e.kind == "directory" else "@" if e.kind == "symlink" else "") for e in result.entries]
        rows.extend(f"{m.path}:{m.line}: {m.text}{' [line truncated]' if m.truncated else ''}" for m in result.matches)
        text = "\n".join(rows) or "(no results)"
        if result.truncated:
            text += "\n[Results incomplete; narrow the path or pattern.]"
        return ToolOutput(text, metadata)
