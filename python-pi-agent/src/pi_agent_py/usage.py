"""Runtime-local accounting of provider-reported usage and estimated USD."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from .domain import AgentEvent, TokenUsage


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens; cached input is a subset of input tokens."""

    input: Decimal
    output: Decimal
    cached_input: Decimal | None = None

    def __post_init__(self) -> None:
        for value in (self.input, self.output, self.cached_input):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError("token prices must be finite and non-negative")

    def estimate(self, usage: TokenUsage) -> Decimal:
        cached = usage.cached_input_tokens or 0
        cache_price = self.input if self.cached_input is None else self.cached_input
        return (
            self.input * (usage.input_tokens - cached)
            + cache_price * cached
            + self.output * usage.output_tokens
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class PricingTable:
    models: Mapping[str, ModelPrice]

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))


@dataclass(frozen=True, slots=True)
class UsageSummary:
    usage: TokenUsage | None = None
    estimated_cost_usd: Decimal | None = None
    unpriced_requests: int = 0

    def _add(self, usage: TokenUsage, cost: Decimal | None) -> UsageSummary:
        current = self.usage
        if current is not None:
            cached = None
            if (
                current.cached_input_tokens is not None
                or usage.cached_input_tokens is not None
            ):
                cached = (current.cached_input_tokens or 0) + (
                    usage.cached_input_tokens or 0
                )
            usage = TokenUsage(
                current.input_tokens + usage.input_tokens,
                current.output_tokens + usage.output_tokens,
                current.total_tokens + usage.total_tokens,
                cached,
            )
        estimate = self.estimated_cost_usd
        if cost is not None:
            estimate = (estimate or Decimal(0)) + cost
        return UsageSummary(usage, estimate, self.unpriced_requests + int(cost is None))


class UsageLedger:
    """Observe finalized reports, retaining session totals and recent run totals."""

    def __init__(
        self,
        pricing: PricingTable | None = None,
        *,
        default_model: str | None = None,
        max_recent_runs: int = 256,
    ) -> None:
        if type(max_recent_runs) is not int or max_recent_runs <= 0:
            raise ValueError("max_recent_runs must be a positive integer")
        self._pricing = pricing
        self._default_model = default_model
        self._max_recent_runs = max_recent_runs
        self._sessions: dict[str, UsageSummary] = {}
        self._active: dict[tuple[str, str], UsageSummary] = {}
        self._recent: OrderedDict[tuple[str, str], UsageSummary] = OrderedDict()

    def for_session(self, session_id: str) -> UsageSummary:
        return self._sessions.get(session_id, UsageSummary())

    def for_run(self, session_id: str, run_id: str) -> UsageSummary | None:
        key = (session_id, run_id)
        return self._active.get(key) or self._recent.get(key)

    def emit(self, event: AgentEvent) -> None:
        session_id = event.envelope.session_id
        run_id = event.envelope.run_id
        if session_id is None or run_id is None:
            return
        key = (session_id, run_id)
        if event.kind == "run_settled":
            self._recent[key] = self._active.pop(key, UsageSummary())
            if len(self._recent) > self._max_recent_runs:
                self._recent.popitem(last=False)
        elif (usage := _reported_usage(event)) is not None:
            model = event.payload.get("model", self._default_model)
            price = (
                self._pricing.models.get(model)
                if self._pricing is not None and isinstance(model, str)
                else None
            )
            cost = price.estimate(usage) if price is not None else None
            self._active[key] = self._active.get(key, UsageSummary())._add(usage, cost)
            self._sessions[session_id] = self.for_session(session_id)._add(usage, cost)


def _token_count(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _reported_usage(event: AgentEvent) -> TokenUsage | None:
    if event.kind != "model_usage":
        return None
    input_tokens = _token_count(event.payload.get("input_tokens"))
    output_tokens = _token_count(event.payload.get("output_tokens"))
    total_tokens = _token_count(event.payload.get("total_tokens"))
    cached = _token_count(event.payload.get("cached_input_tokens"))
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None
    if cached is not None and cached > input_tokens:
        return None
    return TokenUsage(input_tokens, output_tokens, total_tokens, cached)
