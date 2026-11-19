"""The built-in Bash capability definition, parser, and PluginAPI initializer."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from ..capability_catalog import (
    CapabilityConfigContext,
    CapabilityRegistration,
    CapabilitySettings,
)
from ..configuration import _reject_unknown_keys
from ...domain import ToolSideEffects, WorkspaceAccessMode
from ...errors import ConfigurationError
from ...plugins import PluginAPI
from ...tools.sandbox import DockerProcessSandbox
from ...tools.shell import BashTool, SandboxedBashExecutor


_BASH_KEYS = {
    "provider",
    "workspace_root",
    "image",
    "docker_executable",
    "mode",
    "default_timeout_seconds",
    "max_timeout_seconds",
    "max_output_bytes",
    "network_enabled",
}


@dataclass(frozen=True, slots=True)
class DockerBashSettings:
    """Trusted, non-secret Docker settings for the Bash capability."""

    workspace_root: Path
    image: str
    docker_executable: str = "docker"
    mode: WorkspaceAccessMode = "workspace-write"
    default_timeout_seconds: float = 120.0
    max_timeout_seconds: float = 600.0
    max_output_bytes: int = 1_000_000
    network_enabled: bool = False
    provider: Literal["docker"] = field(default="docker", init=False)
    name: Literal["bash"] = field(default="bash", init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.workspace_root, Path)
            or not self.workspace_root.is_absolute()
        ):
            raise ConfigurationError("bash.workspace_root must be an absolute path")
        for name, value in (
            ("image", self.image),
            ("docker_executable", self.docker_executable),
        ):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ConfigurationError(
                    f"bash.{name} must be a non-empty string without NUL"
                )
        if self.mode not in ("read-only", "workspace-write"):
            raise ConfigurationError(
                "bash.mode must be 'read-only' or 'workspace-write'"
            )
        for name, value in (
            ("default_timeout_seconds", self.default_timeout_seconds),
            ("max_timeout_seconds", self.max_timeout_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ConfigurationError(
                    f"bash.{name} must be a positive finite number"
                )
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ConfigurationError(
                "bash.default_timeout_seconds must not exceed "
                "max_timeout_seconds"
            )
        if (
            not isinstance(self.max_output_bytes, int)
            or isinstance(self.max_output_bytes, bool)
            or self.max_output_bytes <= 0
        ):
            raise ConfigurationError(
                "bash.max_output_bytes must be a positive integer"
            )
        if type(self.network_enabled) is not bool:
            raise ConfigurationError("bash.network_enabled must be a boolean")


def _parse(
    raw: Mapping[str, object], context: CapabilityConfigContext
) -> DockerBashSettings:
    value: dict[object, object] = {key: value for key, value in raw.items()}
    config_path = context.config_path
    _reject_unknown_keys(value, _BASH_KEYS, context="Bash")
    if value.get("provider") != "docker":
        raise ConfigurationError("bash.provider must be 'docker'")

    workspace_value = _bash_string(value, "workspace_root", ".")
    try:
        workspace = Path(workspace_value)
        if not workspace.is_absolute():
            workspace = config_path.parent / workspace
        workspace = workspace.resolve()
    except (OSError, ValueError) as error:
        raise ConfigurationError(
            "bash.workspace_root could not be resolved"
        ) from error

    return DockerBashSettings(
        workspace_root=workspace,
        image=_bash_required_string(value, "image"),
        docker_executable=_bash_string(
            value,
            "docker_executable",
            "docker",
        ),
        mode=_bash_mode(value),
        default_timeout_seconds=_bash_positive_number(
            value,
            "default_timeout_seconds",
            120.0,
        ),
        max_timeout_seconds=_bash_positive_number(
            value,
            "max_timeout_seconds",
            600.0,
        ),
        max_output_bytes=_bash_positive_int(
            value,
            "max_output_bytes",
            1_000_000,
        ),
        network_enabled=_bash_boolean(value, "network_enabled", False),
    )


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


def _bash_string(
    values: Mapping[object, object],
    key: str,
    default: str,
) -> str:
    value = values.get(key, default)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigurationError(
            f"bash.{key} must be a non-empty string without NUL"
        )
    return value


def _bash_required_string(
    values: Mapping[object, object],
    key: str,
) -> str:
    if key not in values:
        raise ConfigurationError(f"bash.{key} is required")
    return _bash_string(values, key, "")


def _bash_mode(values: Mapping[object, object]) -> WorkspaceAccessMode:
    value = values.get("mode", "workspace-write")
    if value not in ("read-only", "workspace-write"):
        raise ConfigurationError(
            "bash.mode must be 'read-only' or 'workspace-write'"
        )
    return cast(WorkspaceAccessMode, value)


def _bash_positive_number(
    values: Mapping[object, object],
    key: str,
    default: float,
) -> float:
    value = values.get(key, default)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationError(f"bash.{key} must be a positive finite number")
    return float(value)


def _bash_positive_int(
    values: Mapping[object, object],
    key: str,
    default: int,
) -> int:
    value = values.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"bash.{key} must be a positive integer")
    return value


def _bash_boolean(
    values: Mapping[object, object],
    key: str,
    default: bool,
) -> bool:
    value = values.get(key, default)
    if type(value) is not bool:
        raise ConfigurationError(f"bash.{key} must be a boolean")
    return value
