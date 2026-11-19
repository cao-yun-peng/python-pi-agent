"""Bounded literal text search in workspace files."""

from ...ports import WorkspaceQueries
from ._query_tool import QueryTool


class GrepTool(QueryTool):
    def __init__(self, files: WorkspaceQueries, *, requires_approval: bool = False) -> None:
        super().__init__(files, "grep", "Search UTF-8 files for a case-sensitive literal substring, not regex. Returns file paths, 1-based lines and bounded matching text. Skips binary/large/unreadable files, symlinks and .git/.venv/node_modules/__pycache__; does not parse .gitignore. Narrow the path when incomplete.", requires_approval=requires_approval)
