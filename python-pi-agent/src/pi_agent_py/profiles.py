"""Code-owned product defaults resolved by the application builder."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .ports import (
    ApprovalPolicy,
    CompactionPolicy,
    CompactionStrategy,
    ContextStrategy,
    NextTurnHook,
    Tool,
    ToolExecutionGate,
)
from .tools import DenyByDefaultApprovalPolicy, DeclaredSideEffectGate

ToolFactory = Callable[[], Tool]
ApprovalPolicyFactory = Callable[[], ApprovalPolicy]
ExecutionGateFactory = Callable[[], ToolExecutionGate]
ContextStrategyFactory = Callable[[], ContextStrategy]
CompactionPolicyFactory = Callable[[], CompactionPolicy]
CompactionStrategyFactory = Callable[[], CompactionStrategy]
NextTurnHookFactory = Callable[[], NextTurnHook]


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """Immutable defaults for one product shape, not a container of live I/O."""

    name: str
    system_prompt: str | None = None
    tool_factories: tuple[ToolFactory, ...] = ()
    approval_policy_factory: ApprovalPolicyFactory | None = None
    execution_gate_factory: ExecutionGateFactory | None = None
    context_strategy_factory: ContextStrategyFactory | None = None
    compaction_policy_factory: CompactionPolicyFactory | None = None
    compaction_strategy_factory: CompactionStrategyFactory | None = None
    next_turn_hook_factory: NextTurnHookFactory | None = None
    max_model_requests: int = 8

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("profile name must not be empty")
        if self.system_prompt is not None and not self.system_prompt.strip():
            raise ValueError("profile system_prompt must not be empty")
        if self.max_model_requests <= 0:
            raise ValueError("profile max_model_requests must be greater than zero")
        if (
            self.compaction_policy_factory is not None
            and self.compaction_strategy_factory is None
        ):
            raise ValueError(
                "profile compaction_policy_factory requires "
                "compaction_strategy_factory"
            )


def minimal_profile() -> AgentProfile:
    """Return the smallest safe profile with no tools or product persona."""
    return AgentProfile(name="minimal")


def coding_profile() -> AgentProfile:
    """Return safe policy defaults for an explicitly configured Bash tool."""
    return AgentProfile(
        name="coding",
        approval_policy_factory=DenyByDefaultApprovalPolicy,
        execution_gate_factory=lambda: DeclaredSideEffectGate(
            allowed_side_effects=("none", "read", "write", "external"),
            allowed_tools=("bash",),
        ),
    )
