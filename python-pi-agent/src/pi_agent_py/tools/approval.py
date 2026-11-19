"""Explicit approval policies for protected tool calls."""

from __future__ import annotations

from ..domain import ApprovalDecision, ToolExecutionContext


class DenyByDefaultApprovalPolicy:
    """Secure default used when no interactive approval adapter is configured."""

    async def review(
        self,
        context: ToolExecutionContext,
    ) -> ApprovalDecision:
        del context
        return ApprovalDecision(
            approved=False,
            reason="No approval policy was configured for this protected tool.",
        )


class StaticApprovalPolicy:
    """Deterministic policy useful for tests and pre-authorized applications."""

    def __init__(self, approved: bool, *, reason: str | None = None) -> None:
        self._decision = ApprovalDecision(approved=approved, reason=reason)
        self.requests: list[ToolExecutionContext] = []

    async def review(
        self,
        context: ToolExecutionContext,
    ) -> ApprovalDecision:
        self.requests.append(context)
        return self._decision
