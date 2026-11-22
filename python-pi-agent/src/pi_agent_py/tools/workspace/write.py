"""Create text files, or replace an existing file at an explicitly checked version."""

from ...domain import JSONObject, ToolDefinition, ToolExecutionContext, ToolOutput, WorkspaceWriteRequest
from ...errors import WorkspaceFileError
from ...ports import WorkspaceMutations
from ._mutation_tool import expected_version, mutation_error, mutation_output, text_argument


class WriteTool:
    def __init__(self, files: WorkspaceMutations, *, requires_approval: bool = True) -> None:
        self._files = files
        self._definition = ToolDefinition("write",
            "Create a UTF-8 file and missing parent directories. To overwrite, first read the file and pass its expected_version. "
            "Use write for new files or complete rewrites; use edit for precise changes. Never retry an unknown outcome automatically.",
            {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                "expected_version": {"type": ["string", "null"]}}, "required": ["path", "content"], "additionalProperties": False},
            side_effects="write", requires_approval=requires_approval, retry_safe=False, replay_safe=False, timeout_seconds=35,
            prompt_guidelines=(
                "Use write for new files or full rewrites; read existing files first and provide expected_version when overwriting.",
                "Never automatically retry a file mutation whose outcome is unknown.",
            ))

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        del context
        try:
            request = WorkspaceWriteRequest(text_argument(arguments, "path"), text_argument(arguments, "content"), expected_version(arguments))
        except ValueError:
            return ToolOutput("Invalid write request: use a relative path, UTF-8 content and an optional read version.", is_error=True)
        try:
            return mutation_output(await self._files.write_text(request))
        except WorkspaceFileError as error:
            return mutation_error(error)
