"""Explicit distributed runtime assembly, independent of infrastructure SDKs."""

from dataclasses import dataclass

from ..execution_domain import _text, _integer
from ..ports import SessionExecutionStore, SessionLeaseManager, LeaseClock
from .lease_heartbeat import LeaseHeartbeatSettings


@dataclass(frozen=True, slots=True)
class DistributedExecutionSettings:
    owner_id: str
    recovery_spec_ref: str
    recovery_spec_hash: str
    max_dispatches: int = 40
    max_attempts: int = 5
    heartbeat: LeaseHeartbeatSettings = LeaseHeartbeatSettings()

    def __post_init__(self) -> None:
        for name in ("owner_id", "recovery_spec_ref", "recovery_spec_hash"):
            _text(getattr(self, name), name)
        _integer(self.max_dispatches, "max_dispatches", 1)
        _integer(self.max_attempts, "max_attempts", 1)


@dataclass(frozen=True, slots=True)
class DistributedExecution:
    store: SessionExecutionStore
    leases: SessionLeaseManager
    settings: DistributedExecutionSettings
    clock: LeaseClock | None = None
