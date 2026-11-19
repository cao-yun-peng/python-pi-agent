"""Stable error categories exposed by the harness."""


class AgentError(Exception):
    """Base class for classified harness failures."""


class InternalError(AgentError):
    """An unexpected error occurred inside the agent runtime."""


class ConfigurationError(AgentError):
    """A component was created with invalid or missing configuration."""


class ProviderError(AgentError):
    """The model provider failed before producing a valid response."""


class ProviderAuthenticationError(ProviderError):
    """The provider rejected the supplied credentials."""


class ProviderBalanceError(ProviderError):
    """The provider account does not have enough balance."""


class ProviderRateLimitError(ProviderError):
    """The provider rejected a request because a rate limit was reached."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderTimeoutError(ProviderError):
    """The provider request exceeded its configured timeout."""


class ProviderTransportError(ProviderError):
    """The provider transport failed before a valid response completed."""


class ProviderHTTPError(ProviderError):
    """An HTTP provider returned a non-success response."""

    def __init__(self, status_code: int, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ProviderResponseError(ProviderError):
    """The provider returned a response that could not be normalized."""


class ProviderRoutingError(ProviderError):
    """No configured provider route can satisfy a logical model request."""


class ProviderExhaustedError(ProviderError):
    """Every retryable provider attempt was exhausted without a response."""

    def __init__(self, message: str, *, attempts: tuple[object, ...]) -> None:
        super().__init__(message)
        self.attempts = attempts


class ModelProtocolError(AgentError):
    """The provider stream violated the model event protocol."""


class MiddlewareError(AgentError):
    """A middleware transform failed or violated its stage contract."""


class ContextPreparationError(AgentError):
    """A context strategy failed to prepare a valid model request."""


class ContextBudgetExceededError(ContextPreparationError):
    """Required context cannot fit within the configured token budget."""

    def __init__(self, required_tokens: int, token_budget: int) -> None:
        super().__init__(
            "mandatory context requires "
            f"{required_tokens} tokens but budget is {token_budget}"
        )
        self.required_tokens = required_tokens
        self.token_budget = token_budget


class CompactionError(AgentError):
    """Durable context compaction could not be completed."""


class CompactionNotNeededError(CompactionError):
    """The session does not contain enough completed turns to compact."""


class CompactionPolicyError(CompactionError):
    """Automatic compaction policy evaluation failed."""


class ToolValidationError(AgentError):
    """Tool arguments do not satisfy the declared input schema."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


class ToolExecutionGateError(AgentError):
    """A tool execution gate could not safely dispatch an invocation."""


class ToolExecutionDeniedError(ToolExecutionGateError):
    """A tool execution policy denied the requested invocation."""


class SandboxError(AgentError):
    """An isolated process sandbox failed before producing a result."""


class SandboxUnavailableError(SandboxError):
    """The requested process isolation cannot be enforced on this host."""


class SandboxTimeoutError(SandboxError):
    """A sandboxed process exceeded its deadline and was terminated."""


class PluginError(AgentError):
    """A plugin could not be loaded, executed, or released safely."""


class PluginRegistrationError(PluginError):
    """A plugin declared an invalid or conflicting capability."""


class PluginLoadError(PluginError):
    """A plugin factory failed before its registrations became visible."""


class PluginExecutionError(PluginError):
    """An awaited plugin interceptor failed or exceeded its timeout."""


class PluginCleanupError(PluginError):
    """One or more plugin cleanup callbacks failed during release."""


class AgentLoopLimitError(AgentError):
    """The run exceeded its configured number of model requests."""


class AgentRuntimeError(AgentError):
    """The application runtime could not be built, used, or closed safely."""


class RuntimeBuildError(AgentRuntimeError):
    """Application assembly failed and all created resources were rolled back."""


class RuntimeStateError(AgentRuntimeError):
    """An operation is not valid for the current application runtime state."""


class RuntimeCapacityError(AgentRuntimeError):
    """No inactive runtime session can be evicted to satisfy capacity."""


class RuntimeCloseError(AgentRuntimeError):
    """One or more resources failed while the runtime continued shutting down."""

    def __init__(
        self,
        message: str,
        *,
        failures: tuple[BaseException, ...],
    ) -> None:
        super().__init__(message)
        self.failures = failures


class SessionConflictError(AgentError):
    """The session changed after the caller last loaded it."""


class SessionValidationError(AgentError):
    """Session input does not satisfy the storage contract."""


class SessionControlError(AgentError):
    """A control command is invalid for the current session state."""


class SessionReplayError(AgentError):
    """A durable session log cannot be replayed safely."""


class SessionStorageError(AgentError):
    """A session store could not read or persist durable data."""


def _error_category(error: AgentError) -> str:
    """Return a stable run-event category, including for adapter subclasses."""
    categories = (
        (InternalError, "internal"),
        (ConfigurationError, "configuration"),
        (ProviderAuthenticationError, "authentication"),
        ((ProviderTimeoutError, SandboxTimeoutError), "timeout"),
        (ProviderError, "provider"),
        (ModelProtocolError, "protocol"),
        (SessionStorageError, "storage"),
        (
            (
                SessionConflictError,
                SessionValidationError,
                SessionControlError,
                SessionReplayError,
            ),
            "session",
        ),
        (ToolValidationError, "tool_validation"),
        (ToolExecutionDeniedError, "permission"),
        (ToolExecutionGateError, "tool_execution"),
        (SandboxError, "sandbox"),
        (ContextPreparationError, "context"),
        (MiddlewareError, "middleware"),
        (CompactionError, "compaction"),
        (PluginError, "plugin"),
        ((AgentLoopLimitError, AgentRuntimeError), "runtime"),
    )
    for error_types, category in categories:
        if isinstance(error, error_types):
            return category
    return "agent"
