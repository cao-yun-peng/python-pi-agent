"""Resolved Session specifications and the default static resolver."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields

from .._metadata import _freeze_metadata
from .components import AgentComponents
from ..domain import JSONValue, SessionOpenRequest


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """A complete component selection and detached observation metadata."""

    components: AgentComponents
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.components, AgentComponents):
            raise TypeError("SessionSpec requires AgentComponents")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))

    def _matches(self, other: SessionSpec) -> bool:
        """Compare configuration values and capability identities, never port equality."""
        for component in fields(AgentComponents):
            first = getattr(self.components, component.name)
            second = getattr(other.components, component.name)
            if component.name in {"system_prompt", "max_model_requests"}:
                if first != second:
                    return False
            elif component.name in {"tools", "middleware"}:
                if len(first) != len(second) or any(
                    first_tool is not second_tool
                    for first_tool, second_tool in zip(first, second)
                ):
                    return False
            elif component.name == "compaction":
                if first is not second and (
                    first is None
                    or second is None
                    or first.strategy is not second.strategy
                    or first.policy is not second.policy
                ):
                    return False
            elif first is not second:
                return False
        return self.metadata == other.metadata


class StaticSessionSpecResolver:
    """Reuse the application's assembled components with empty observation metadata."""

    def __init__(self, components: AgentComponents) -> None:
        self._spec = SessionSpec(components)

    async def resolve(
        self, session_id: str, request: SessionOpenRequest
    ) -> SessionSpec:
        return self._spec
