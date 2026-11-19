"""Typed configuration definitions adapted to the transactional PluginHost."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .errors import ConfigurationError
from .plugins import PluginAPI, PluginFactory


class CapabilitySettings(Protocol):
    """Validated deployment values owned by one registered capability."""

    @property
    def name(self) -> str: ...


@dataclass(frozen=True, slots=True)
class CapabilityConfigContext:
    config_path: Path

    def __post_init__(self) -> None:
        if not self.config_path.is_absolute():
            raise ValueError("capability config_path must be absolute")


@dataclass(frozen=True, slots=True)
class ConfiguredCapability:
    name: str
    settings: CapabilitySettings

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigurationError("capability name must be a non-empty string")
        if getattr(self.settings, "name", None) != self.name:
            raise ConfigurationError(
                "capability settings name must match its registration"
            )


CapabilitySettingsParser = Callable[
    [Mapping[str, object], CapabilityConfigContext], CapabilitySettings
]
CapabilityFactory = Callable[[CapabilitySettings, PluginAPI], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class CapabilityRegistration:
    name: str
    factory: CapabilityFactory
    parse_settings: CapabilitySettingsParser

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("capability registration name must be a non-empty string")
        if not callable(self.factory) or not callable(self.parse_settings):
            raise TypeError("capability factory and parser must be callable")
        if inspect.iscoroutinefunction(
            self.parse_settings
        ) or inspect.iscoroutinefunction(
            getattr(self.parse_settings, "__call__", None)
        ):
            raise TypeError("capability settings parser must be synchronous")


class CapabilityCatalog:
    def __init__(self, registrations: Sequence[CapabilityRegistration]) -> None:
        values = tuple(registrations)
        if len({item.name for item in values}) != len(values):
            raise ValueError("capability registration names must be unique")
        self._registrations: Mapping[str, CapabilityRegistration] = MappingProxyType(
            {item.name: item for item in values}
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._registrations)

    def with_registration(
        self, registration: CapabilityRegistration
    ) -> CapabilityCatalog:
        return CapabilityCatalog((*self._registrations.values(), registration))

    def parse_settings(
        self, name: str, values: Mapping[str, object], context: CapabilityConfigContext
    ) -> ConfiguredCapability:
        registration = self._get(name)
        try:
            settings = registration.parse_settings(
                MappingProxyType(deepcopy(dict(values))), context
            )
        except ConfigurationError:
            raise
        except Exception as error:
            raise ConfigurationError("capability settings parser failed") from error
        return ConfiguredCapability(name, settings)

    def plugin_for(self, capability: ConfiguredCapability) -> PluginFactory:
        """Validate selection before allocating resources, then defer to PluginHost."""
        registration = self._get(capability.name)

        def initialize(api: PluginAPI) -> Awaitable[None] | None:
            return registration.factory(capability.settings, api)

        return initialize

    def _get(self, name: str) -> CapabilityRegistration:
        registration = self._registrations.get(name)
        if registration is None:
            raise ConfigurationError(
                "capability name is not registered in this application"
            )
        return registration


def default_capability_catalog() -> CapabilityCatalog:
    from .configured_bash import bash_capability_registration

    return CapabilityCatalog((bash_capability_registration(),))
