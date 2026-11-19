"""Model-facing UTF-8 read tool over the WorkspaceFiles port."""

from __future__ import annotations

from ..domain import JSONObject, ToolDefinition, ToolExecutionContext, ToolOutput, WorkspaceReadRequest
from ..errors import WorkspaceFileError
from ..ports import WorkspaceFiles


class ReadTool:
    def __init__(self, files: WorkspaceFiles, *, requires_approval: bool = False) -> None:
        self._files = files
        self._definition = ToolDefinition(
            "read", "Read UTF-8 text inside the configured workspace. Use a relative POSIX path. "
            "offset is a 1-based line number; limit defaults to 200 lines. Output is bounded; "
            "continue using next_offset. Symbolic links and binary files are rejected. "
            "Use read to inspect files before changing them.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "offset": {"type": "integer", "description": "Starting line, 1-based.", "default": 1},
                "limit": {"type": "integer", "description": "Requested line count, capped by the backend.", "default": 200},
            }, "required": ["path"], "additionalProperties": False},
            side_effects="read", requires_approval=requires_approval,
            retry_safe=True, replay_safe=False, timeout_seconds=35.0,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        del context
        try:
            path, offset, limit = arguments.get("path"), arguments.get("offset", 1), arguments.get("limit", 200)
            if not isinstance(path, str) or type(offset) is not int or type(limit) is not int:
                raise ValueError("invalid read arguments")
            request = WorkspaceReadRequest(path, offset, limit)
        except ValueError:
            return ToolOutput("Invalid read request: use a workspace-relative POSIX path and positive integer offset/limit.", is_error=True)
        try:
            result = await self._files.read_text(request)
        except WorkspaceFileError as error:
            return ToolOutput(str(error), {"code": error.code}, is_error=True)
        metadata: JSONObject = {
            "path": result.path, "offset": result.offset, "returned_lines": result.returned_lines,
            "total_lines": result.total_lines, "total_bytes": result.total_bytes,
            "version": result.version, "next_offset": result.next_offset,
        }
        suffix = f"\n[More lines available; continue with offset={result.next_offset}.]" if result.next_offset is not None else ""
        return ToolOutput(result.text + suffix or "(empty page)", metadata)
