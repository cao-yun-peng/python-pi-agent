"""The built-in Bash capability definition, parser, and PluginAPI initializer."""

from __future__ import annotations

from collections.abc import Mapping

from ..capability_catalog import (
    CapabilityConfigContext,
    CapabilityRegistration,
    CapabilitySettings,
)
from ..configuration import DockerBashSettings, _parse_bash
from ...domain import ToolSideEffects
from ...errors import ConfigurationError
from ...plugins import PluginAPI
from ...tools.sandbox import DockerProcessSandbox
from ...tools.shell import BashTool, SandboxedBashExecutor


def _parse(
    raw: Mapping[str, object], context: CapabilityConfigContext
) -> DockerBashSettings:
    values: dict[object, object] = {key: value for key, value in raw.items()}
    settings = _parse_bash(values, config_path=context.config_path)
    if settings is None:
        raise ConfigurationError("bash settings are required")
    return settings


def _initialize(settings: CapabilitySettings, api: PluginAPI) -> None:
    if not isinstance(settings, DockerBashSettings):
        raise ConfigurationError("bash capability requires DockerBashSettings")
    sandbox = DockerProcessSandbox(
        settings.workspace_root,
        image=settings.image,
        docker_executable=settings.docker_executable,
        network_enabled=settings.network_enabled,
        max_output_bytes=settings.max_output_bytes,
    )
    executor = SandboxedBashExecutor(
        sandbox,
        mode=settings.mode,
        default_timeout_seconds=settings.default_timeout_seconds,
        max_timeout_seconds=settings.max_timeout_seconds,
    )
    side_effects: ToolSideEffects
    if settings.network_enabled:
        side_effects = "external"
    elif settings.mode == "read-only":
        side_effects = "read"
    else:
        side_effects = "write"
    api.register_tool(
        BashTool(
            executor,
            requires_approval=True,
            side_effects=side_effects,
        )
    )


def bash_capability_registration() -> CapabilityRegistration:
    return CapabilityRegistration("bash", _initialize, parse_settings=_parse)
