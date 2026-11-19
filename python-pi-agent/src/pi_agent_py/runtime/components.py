"""Resolved, immutable configuration for model-visible agent behavior."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Self, TypedDict, Unpack

from ..context.strategies import FullHistoryContextStrategy
from ..errors import ConfigurationError
from ..ports import (
    ApprovalPolicy,
    CompactionPolicy,
    CompactionStrategy,
    ContextStrategy,
    ModelProvider,
    NextTurnHook,
    Tool,
    ToolExecutionGate,
)
from ..tools.approval import DenyByDefaultApprovalPolicy
from ..tools.gates import DeclaredSideEffectGate


_DEFAULT_MAX_MODEL_REQUESTS = 8


@dataclass(frozen=True, slots=True)
class CompactionComponents:
    """A summary strategy, optionally triggered by an automatic policy."""

    strategy: CompactionStrategy
    policy: CompactionPolicy | None = None

    def __post_init__(self) -> None:
        if self.strategy is None:
            raise ConfigurationError("compaction requires a compaction strategy")


class _ComponentChanges(TypedDict, total=False):
    provider: ModelProvider
    tools: tuple[Tool, ...]
    approval_policy: ApprovalPolicy
    execution_gate: ToolExecutionGate
    context_strategy: ContextStrategy
    compaction: CompactionComponents | None
    next_turn_hook: NextTurnHook | None
    system_prompt: str | None
    max_model_requests: int


@dataclass(frozen=True, slots=True)
class AgentComponents:
    """The resolved options and ports consumed by an Agent or Session."""

    provider: ModelProvider
    tools: tuple[Tool, ...] = ()
    approval_policy: ApprovalPolicy = field(default_factory=DenyByDefaultApprovalPolicy)
    execution_gate: ToolExecutionGate = field(default_factory=DeclaredSideEffectGate)
    context_strategy: ContextStrategy = field(default_factory=FullHistoryContextStrategy)
    compaction: CompactionComponents | None = None
    next_turn_hook: NextTurnHook | None = None
    system_prompt: str | None = None
    max_model_requests: int = _DEFAULT_MAX_MODEL_REQUESTS

    def __post_init__(self) -> None:
        if type(self.max_model_requests) is not int or self.max_model_requests <= 0:
            raise ValueError("max_model_requests must be a positive integer")
        tools = tuple(self.tools)
        object.__setattr__(self, "tools", tools)
        counts = Counter(tool.definition.name for tool in tools)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError("duplicate tool names: " + ", ".join(duplicates))

    def replace(self, **changes: Unpack[_ComponentChanges]) -> Self:
        """Return a validated copy with the supplied fields replaced."""
        return replace(self, **changes)
