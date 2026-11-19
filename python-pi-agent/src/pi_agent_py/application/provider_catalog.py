"""Explicit provider registrations for settings parsing and resource construction."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeVar

from .configuration import (
    ProviderRuntimeConfig,
    ProviderSettings,
    _parse_deepseek_settings,
    _parse_glm_settings,
    _parse_kimi_settings,
    _parse_timicc_settings,
    _validate_env_name,
    _validated_builtin_settings,
)
from ..errors import ConfigurationError
from ..ports import ModelProvider
from ..providers import (
    DeepSeekConfig,
    DeepSeekProvider,
    GLMConfig,
    GLMProvider,
    KimiConfig,
    KimiProvider,
    TimiccResponsesConfig,
    TimiccResponsesProvider,
)

ProviderFactory = Callable[
    [ProviderRuntimeConfig], ModelProvider | Awaitable[ModelProvider]
]
ProviderSettingsParser = Callable[[Mapping[str, object]], ProviderSettings]
ConfigT = TypeVar("ConfigT")


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """A pure settings parser and an optionally asynchronous Provider factory."""

    name: str
    factory: ProviderFactory
    supports_tools: bool = True
    state_owners: frozenset[str] = frozenset()
    parse_settings: ProviderSettingsParser | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("provider registration name must not be empty")
        if not callable(self.factory):
            raise TypeError("provider factory must be callable")
        if self.parse_settings is not None and not callable(self.parse_settings):
            raise TypeError("provider settings parser must be callable")
        if inspect.iscoroutinefunction(
            self.parse_settings
        ) or inspect.iscoroutinefunction(
            getattr(self.parse_settings, "__call__", None)
        ):
            raise TypeError("provider settings parser must be synchronous")
        object.__setattr__(self, "state_owners", frozenset(self.state_owners))


@dataclass(frozen=True, slots=True)
class BuiltProvider:
    name: str
    provider: ModelProvider
    supports_tools: bool
    state_owners: frozenset[str]


class ProviderCatalog:
    """Immutable parser/factory selection scoped to one application."""

    def __init__(self, registrations: Sequence[ProviderRegistration]) -> None:
        values = tuple(registrations)
        names = [item.name for item in values]
        if len(set(names)) != len(names):
            raise ValueError("provider registration names must be unique")
        self._registrations: Mapping[str, ProviderRegistration] = MappingProxyType(
            {item.name: item for item in values}
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._registrations)

    def with_registration(self, registration: ProviderRegistration) -> ProviderCatalog:
        """Return a new catalog, rejecting duplicate names."""
        return ProviderCatalog((*self._registrations.values(), registration))

    def parse_settings(
        self, name: str, values: Mapping[str, object]
    ) -> ProviderSettings:
        if "api_key" in values:
            raise ConfigurationError(
                "inline provider.api_key is not supported; use provider.api_key_env "
                "and store the secret in the selected .env file or process environment"
            )
        if values.get("name", name) != name:
            raise ConfigurationError("provider.name does not match the registration")
        registration = self._registrations.get(name)
        if registration is None:
            raise ConfigurationError(
                "provider.name is not registered in this application"
            )
        parser = registration.parse_settings
        if parser is None:
            raise ConfigurationError("provider registration has no settings parser")
        try:
            settings = parser(MappingProxyType(deepcopy(dict(values))))
        except ConfigurationError:
            raise
        except Exception as error:
            raise ConfigurationError("provider settings parser failed") from error
        if getattr(settings, "name", None) != name:
            raise ConfigurationError(
                "provider parser returned settings with a different name"
            )
        variable = getattr(settings, "api_key_env", None)
        if not isinstance(variable, str):
            raise ConfigurationError("provider settings must declare api_key_env")
        _validate_env_name(variable)
        if (
            not isinstance(getattr(settings, "model", None), str)
            or not settings.model.strip()
        ):
            raise ConfigurationError("provider settings must declare a non-empty model")
        if not callable(getattr(settings, "build", None)):
            raise ConfigurationError("provider settings must implement build")
        return settings

    async def build(self, name: str, config: ProviderRuntimeConfig) -> BuiltProvider:
        registration = self._registrations.get(name)
        if registration is None:
            raise ConfigurationError(
                f'provider "{name}" is not registered in this application'
            )
        value = registration.factory(config)
        provider = await value if inspect.isawaitable(value) else value
        if not callable(getattr(provider, "stream", None)):
            raise TypeError("provider factory must return a ModelProvider")
        return BuiltProvider(
            name=name,
            provider=provider,
            supports_tools=registration.supports_tools,
            state_owners=registration.state_owners,
        )


def default_provider_catalog() -> ProviderCatalog:
    """Build a fresh catalog using the same parser contract as extensions."""
    return ProviderCatalog(
        (
            ProviderRegistration(
                "deepseek",
                lambda config: DeepSeekProvider(
                    _expect_config(config, DeepSeekConfig, "deepseek")
                ),
                parse_settings=lambda raw: _validated_builtin_settings(
                    _parse_deepseek_settings(raw)
                ),
            ),
            ProviderRegistration(
                "kimi",
                lambda config: KimiProvider(_expect_config(config, KimiConfig, "kimi")),
                parse_settings=lambda raw: _validated_builtin_settings(
                    _parse_kimi_settings(raw)
                ),
            ),
            ProviderRegistration(
                "glm",
                lambda config: GLMProvider(_expect_config(config, GLMConfig, "glm")),
                parse_settings=lambda raw: _validated_builtin_settings(
                    _parse_glm_settings(raw)
                ),
            ),
            ProviderRegistration(
                "timicc",
                lambda config: TimiccResponsesProvider(
                    _expect_config(config, TimiccResponsesConfig, "timicc")
                ),
                state_owners=frozenset({"timicc-responses"}),
                parse_settings=lambda raw: _validated_builtin_settings(
                    _parse_timicc_settings(raw)
                ),
            ),
        )
    )


def _expect_config(
    config: ProviderRuntimeConfig, expected: type[ConfigT], provider_name: str
) -> ConfigT:
    if not isinstance(config, expected):
        raise ConfigurationError(
            f'provider "{provider_name}" received incompatible configuration'
        )
    return config
