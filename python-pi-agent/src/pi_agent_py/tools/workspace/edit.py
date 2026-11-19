"""Replace one unique exact text region in a workspace file."""

from ...domain import JSONObject, ToolDefinition, ToolExecutionContext, ToolOutput, WorkspaceEditRequest
from ...errors import WorkspaceFileError
from ...ports import WorkspaceMutations
from ._mutation_tool import expected_version, mutation_error, mutation_output, text_argument


class EditTool:
    def __init__(self, files: WorkspaceMutations, *, requires_approval: bool = True) -> None:
        self._files = files
        self._definition = ToolDefinition("edit",
            "Make one exact unique text replacement in a UTF-8 file. old_text must be nonempty and occur exactly once. "
            "Read first and pass expected_version to detect stale content. BOM and untouched line endings are preserved. "
            "Never retry an unknown outcome automatically.",
            {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                "new_text": {"type": "string"}, "expected_version": {"type": ["string", "null"]}},
                "required": ["path", "old_text", "new_text"], "additionalProperties": False},
            side_effects="write", requires_approval=requires_approval, retry_safe=False, replay_safe=False, timeout_seconds=35)

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        del context
        try:
            request = WorkspaceEditRequest(text_argument(arguments, "path"), text_argument(arguments, "old_text"),
                text_argument(arguments, "new_text"), expected_version(arguments))
        except ValueError:
            return ToolOutput("Invalid edit request: use a relative path, nonempty old_text, UTF-8 new_text and an optional read version.", is_error=True)
        try:
            return mutation_output(await self._files.edit_text(request))
        except WorkspaceFileError as error:
            return mutation_error(error)
