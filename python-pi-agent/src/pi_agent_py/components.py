"""Resolved, immutable configuration for model-visible agent behavior."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Self, TypedDict, Unpack
from warnings import warn

from .context.strategies import FullHistoryContextStrategy
from .errors import ConfigurationError
from .ports import (
    ApprovalPolicy,
    CompactionPolicy,
    CompactionStrategy,
    ContextStrategy,
    ModelProvider,
    NextTurnHook,
    Tool,
    ToolExecutionGate,
)
from .tools import DeclaredSideEffectGate, DenyByDefaultApprovalPolicy


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


def _resolve_components(
    provider: AgentComponents | ModelProvider | None,
    components: AgentComponents | None,
    *,
    tools: Sequence[Tool] | None,
    approval_policy: ApprovalPolicy | None,
    execution_gate: ToolExecutionGate | None,
    context_strategy: ContextStrategy | None,
    compaction_policy: CompactionPolicy | None = None,
    compaction_strategy: CompactionStrategy | None = None,
    next_turn_hook: NextTurnHook | None,
    system_prompt: str | None,
    max_model_requests: int | None,
) -> AgentComponents:
    """Adapt the legacy constructors at their public boundary."""
    if isinstance(provider, AgentComponents):
        if components is not None:
            raise TypeError("components was supplied twice")
        components, provider = provider, None
    if components is not None:
        legacy_options = (
            provider, tools, approval_policy, execution_gate, context_strategy,
            compaction_policy, compaction_strategy, next_turn_hook, system_prompt,
            max_model_requests,
        )
        if any(value is not None for value in legacy_options):
            raise TypeError("components cannot be combined with legacy options")
        return components
    if provider is None:
        raise TypeError("an AgentComponents instance or provider is required")
    warn(
        "pass AgentComponents instead of individual options",
        DeprecationWarning,
        stacklevel=3,
    )
    if compaction_policy is not None and compaction_strategy is None:
        raise ConfigurationError("compaction_policy requires a compaction_strategy")
    return AgentComponents(
        provider=provider,
        tools=tuple(tools) if tools is not None else (),
        approval_policy=(
            approval_policy if approval_policy is not None else DenyByDefaultApprovalPolicy()
        ),
        execution_gate=(
            execution_gate if execution_gate is not None else DeclaredSideEffectGate()
        ),
        context_strategy=context_strategy or FullHistoryContextStrategy(),
        compaction=(
            CompactionComponents(compaction_strategy, compaction_policy)
            if compaction_strategy is not None else None
        ),
        next_turn_hook=next_turn_hook,
        system_prompt=system_prompt,
        max_model_requests=(
            max_model_requests
            if max_model_requests is not None
            else _DEFAULT_MAX_MODEL_REQUESTS
        ),
    )
