"""Stable error categories exposed by the harness."""


class AgentError(Exception):
    """Base class for expected harness failures."""


class ProviderError(AgentError):
    """The model provider failed before producing a valid response."""


class ModelProtocolError(AgentError):
    """The provider stream violated the model event protocol."""
