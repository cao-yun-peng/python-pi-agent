"""Content-free telemetry projection and structured logging adapter."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, replace

from .domain import AgentEvent, TelemetryEvent
from .usage import _reported_usage

_NAME = re.compile(r"[a-zA-Z0-9_.:/-]{1,80}")
_OUTCOMES = frozenset(
    {
        "success",
        "error",
        "cancelled",
        "timeout",
        "execution_denied",
        "sandbox_timeout",
        "sandbox_error",
    }
)


def _dimension(value: object) -> str | None:
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def _project(event: AgentEvent) -> TelemetryEvent:
    kind = event.kind
    outcome = event.payload.get("outcome")
    provider = (
        _dimension(event.payload.get("provider_name", event.payload.get("to_provider")))
        if kind
        in {
            "provider_attempt_started",
            "provider_attempt_succeeded",
            "provider_attempt_failed",
            "provider_retry_scheduled",
            "provider_fallback_selected",
        }
        else None
    )
    tool = (
        _dimension(event.payload.get("tool_name"))
        if kind in {"tool_execution_started", "tool_execution_finished"}
        else None
    )
    return TelemetryEvent(
        kind,
        event.channel,
        replace(event.envelope, metadata={}) if event.envelope.metadata else event.envelope,
        outcome=outcome if isinstance(outcome, str) and outcome in _OUTCOMES else None,
        provider_name=provider,
        tool_name=tool,
        usage=_reported_usage(event),
    )


class LoggingTelemetrySink:
    """Write structured records; use QueueHandler for application I/O handlers."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = (
            logger if logger is not None else logging.getLogger("pi_agent_py.telemetry")
        )

    def on_event(self, event: TelemetryEvent) -> None:
        if self._logger.isEnabledFor(logging.INFO):
            record = asdict(event)
            record["envelope"].pop("metadata")
            self._logger.info(event.kind, extra={"pi_agent": record})

    async def aclose(self) -> None:
        """The application owns its logger and handlers."""
