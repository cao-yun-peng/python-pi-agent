"""SWE-bench task projection and replaceable task-environment contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ...ports import ShellExecutor


@dataclass(frozen=True)
class CodingTask:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    image: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+", self.instance_id):
            raise ValueError("invalid instance_id")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("invalid repository")
        if not re.fullmatch(r"[a-f0-9]{40}", self.base_commit):
            raise ValueError("base_commit must be a full commit hash")
        if not self.problem_statement.strip():
            raise ValueError("problem_statement is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", self.image):
            raise ValueError("invalid Docker image reference")

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> CodingTask:
        """Allowlist task inputs; gold/test patches never enter this value."""
        fields = ("instance_id", "repo", "base_commit", "problem_statement", "image")
        values = [row.get(name) for name in fields]
        if any(not isinstance(value, str) for value in values):
            raise ValueError("task fields must be strings (current dataset schema required)")
        return cls(**{name: str(value) for name, value in zip(fields, values)})


class TaskEnvironment(ShellExecutor, Protocol):
    """Own one task filesystem; shell commands share files, not shell state."""

    async def start(self, task: CodingTask) -> str:
        """Prepare base_commit; return immutable environment identity."""
        ...

    async def export_patch(self) -> str:
        """Collect tracked and new files; never return a truncated patch."""
        ...

    async def close(self) -> None:
        """Remove task processes and filesystem, including after cancellation."""
        ...
