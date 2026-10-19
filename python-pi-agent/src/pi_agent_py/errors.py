"""Stable error categories exposed by the harness."""


class AgentError(Exception):
    """Base class for expected harness failures."""


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


class ProviderTimeoutError(ProviderError):
    """The provider request exceeded its configured timeout."""


class ProviderHTTPError(ProviderError):
    """An HTTP provider returned a non-success response."""

    def __init__(self, status_code: int, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ProviderResponseError(ProviderError):
    """The provider returned a response that could not be normalized."""


class ModelProtocolError(AgentError):
    """The provider stream violated the model event protocol."""


class ToolValidationError(AgentError):
    """Tool arguments do not satisfy the declared input schema."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


class AgentLoopLimitError(AgentError):
    """The run exceeded its configured number of model requests."""


class SessionConflictError(AgentError):
    """The session changed after the caller last loaded it."""


class SessionValidationError(AgentError):
    """Session input does not satisfy the storage contract."""


class SessionReplayError(AgentError):
    """A durable session log cannot be replayed safely."""


class SessionStorageError(AgentError):
    """A session store could not read or persist durable data."""
