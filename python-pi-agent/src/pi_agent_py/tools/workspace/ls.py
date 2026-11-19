"""Non-recursive workspace directory listing."""

from ...ports import WorkspaceQueries
from ._query_tool import QueryTool


class LsTool(QueryTool):
    def __init__(self, files: WorkspaceQueries, *, requires_approval: bool = False) -> None:
        super().__init__(files, "ls", "List one workspace directory in name order. Symlinks are marked, never followed. Output is bounded; narrow the path if incomplete.", requires_approval=requires_approval)
