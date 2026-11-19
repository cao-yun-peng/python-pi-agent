"""Code-owned product defaults resolved by the application builder."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..runtime.components import (
    AgentComponents,
    CompactionComponents,
    _DEFAULT_MAX_MODEL_REQUESTS,
)
from ..context.strategies import FullHistoryContextStrategy
from ..ports import (
    ApprovalPolicy,
    CompactionPolicy,
    CompactionStrategy,
    ContextStrategy,
    ModelProvider,
    NextTurnHook,
    SchemaValidator,
    Tool,
    ToolExecutionGate,
)
from ..tools.approval import DenyByDefaultApprovalPolicy
from ..tools.gates import DeclaredSideEffectGate
from ..runtime.middleware import Middleware
from ..tools.schema import SubsetSchemaValidator

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
    max_model_requests: int = _DEFAULT_MAX_MODEL_REQUESTS
    middleware_factories: tuple[Callable[[], Middleware], ...] = ()
    schema_validator_factory: Callable[[], SchemaValidator] | None = None

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

    def build_components(self, provider: ModelProvider) -> AgentComponents:
        """Resolve the factories once at the application assembly boundary."""
        tools = tuple(factory() for factory in self.tool_factories)
        approval = self.approval_policy_factory
        gate = self.execution_gate_factory
        context = self.context_strategy_factory
        approval_policy = approval() if approval is not None else DenyByDefaultApprovalPolicy()
        execution_gate = gate() if gate is not None else DeclaredSideEffectGate()
        context_strategy = context() if context is not None else FullHistoryContextStrategy()
        policy = self.compaction_policy_factory
        compaction_policy = policy() if policy is not None else None
        strategy = self.compaction_strategy_factory
        compaction = (
            CompactionComponents(strategy(), compaction_policy)
            if strategy is not None
            else None
        )
        hook = self.next_turn_hook_factory
        validator_factory = self.schema_validator_factory
        return AgentComponents(
            schema_validator=(validator_factory() if validator_factory is not None else SubsetSchemaValidator()),
            middleware=tuple(factory() for factory in self.middleware_factories),
            provider=provider,
            tools=tools,
            approval_policy=approval_policy,
            execution_gate=execution_gate,
            context_strategy=context_strategy,
            compaction=compaction,
            next_turn_hook=hook() if hook is not None else None,
            system_prompt=self.system_prompt,
            max_model_requests=self.max_model_requests,
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
