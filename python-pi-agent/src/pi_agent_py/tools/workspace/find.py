"""Bounded glob matching of workspace file names."""

from ...ports import WorkspaceQueries
from ._query_tool import QueryTool


class FindTool(QueryTool):
    def __init__(self, files: WorkspaceQueries, *, requires_approval: bool = False) -> None:
        super().__init__(files, "find", "Find files using a case-sensitive glob, e.g. *.py. A pattern containing / matches path segments; ** matches zero or more directories. Skips symlinks and .git/.venv/node_modules/__pycache__; does not parse .gitignore. Results and traversal are bounded.", requires_approval=requires_approval)
