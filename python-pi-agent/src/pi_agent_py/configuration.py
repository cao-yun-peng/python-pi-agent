"""Explicit application-boundary configuration for registered providers.

YAML contains non-secret provider settings and the name of an environment
variable. Secrets are resolved separately from an explicit override, the
process environment, or one explicitly selected dotenv file.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias, TypeVar, cast

import yaml

if TYPE_CHECKING:
    from .provider_catalog import ProviderCatalog

from .domain import WorkspaceAccessMode
from .errors import ConfigurationError
from .providers import (
    DeepSeekConfig,
    GLMConfig,
    GLMReasoningEffort,
    GLMThinkingMode,
    KimiConfig,
    KimiReasoningEffort,
    KimiThinkingKeep,
    KimiThinkingMode,
    ReasoningEffort,
    TimiccReasoningEffort,
    TimiccReasoningSummary,
    TimiccResponsesConfig,
    ThinkingMode,
)
from .providers._provider_support import (
    DEEPSEEK_DEFAULT_API_KEY_ENV,
    DEEPSEEK_DEFAULT_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_DEFAULT_THINKING,
    DEFAULT_TIMEOUT_SECONDS,
    GLM_DEFAULT_API_KEY_ENV,
    GLM_DEFAULT_BASE_URL,
    GLM_DEFAULT_MODEL,
    KIMI_DEFAULT_API_KEY_ENV,
    KIMI_DEFAULT_BASE_URL,
    KIMI_DEFAULT_MODEL,
    TIMICC_DEFAULT_API_KEY_ENV,
    TIMICC_DEFAULT_BASE_URL,
)

ProviderName: TypeAlias = str
CredentialSource: TypeAlias = Literal["explicit", "process_env", "dotenv"]


class ProviderRuntimeConfig(Protocol):
    """Common read-only model identity on an adapter's typed runtime config."""

    @property
    def model(self) -> str: ...


class ProviderSettings(Protocol):
    """Validated non-secret settings, resolved before a Provider is constructed."""

    @property
    def name(self) -> str: ...

    @property
    def api_key_env(self) -> str: ...

    @property
    def model(self) -> str: ...

    def build(self, api_key: str) -> ProviderRuntimeConfig:
        """Build a config value without creating I/O resources."""
        ...


SettingsT = TypeVar("SettingsT", bound=ProviderSettings)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTENV_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)
_ROOT_KEYS = {"version", "provider", "resilience", "bash"}
_RESILIENCE_KEYS = {
    "enabled",
    "max_attempts_per_provider",
    "max_total_attempts",
    "max_elapsed_seconds",
    "base_delay_seconds",
    "max_delay_seconds",
}
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
_COMMON_PROVIDER_KEYS = {
    "name",
    "api_key_env",
    "model",
    "base_url",
    "timeout_seconds",
    "max_tokens",
    "thinking",
    "reasoning_effort",
}
_DEEPSEEK_KEYS = _COMMON_PROVIDER_KEYS | {"user_id"}
_KIMI_KEYS = _COMMON_PROVIDER_KEYS | {"thinking_keep"}
_GLM_KEYS = _COMMON_PROVIDER_KEYS | {"user_id"}
_TIMICC_KEYS = {
    "name",
    "api_key_env",
    "model",
    "base_url",
    "timeout_seconds",
    "reasoning_effort",
    "reasoning_summary",
    "max_output_tokens",
}


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    """Non-secret DeepSeek settings loaded from YAML."""

    api_key_env: str = DEEPSEEK_DEFAULT_API_KEY_ENV
    model: str = DEEPSEEK_DEFAULT_MODEL
    base_url: str = DEEPSEEK_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    thinking: ThinkingMode = DEEPSEEK_DEFAULT_THINKING
    reasoning_effort: ReasoningEffort | None = None
    max_tokens: int | None = None
    user_id: str | None = None
    name: Literal["deepseek"] = field(default="deepseek", init=False)

    def build(self, api_key: str) -> DeepSeekConfig:
        """Create the runtime provider config after credential resolution."""
        return DeepSeekConfig(
            api_key=api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            thinking=self.thinking,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
            user_id=self.user_id,
        )


@dataclass(frozen=True, slots=True)
class KimiSettings:
    """Non-secret Kimi settings loaded from YAML."""

    api_key_env: str = KIMI_DEFAULT_API_KEY_ENV
    model: str = KIMI_DEFAULT_MODEL
    base_url: str = KIMI_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    thinking: KimiThinkingMode | None = None
    thinking_keep: KimiThinkingKeep | None = None
    reasoning_effort: KimiReasoningEffort | None = None
    max_tokens: int | None = None
    name: Literal["kimi"] = field(default="kimi", init=False)

    def build(self, api_key: str) -> KimiConfig:
        """Create the runtime provider config after credential resolution."""
        return KimiConfig(
            api_key=api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            thinking=self.thinking,
            thinking_keep=self.thinking_keep,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
        )


@dataclass(frozen=True, slots=True)
class GLMSettings:
    """Non-secret GLM settings loaded from YAML."""

    api_key_env: str = GLM_DEFAULT_API_KEY_ENV
    model: str = GLM_DEFAULT_MODEL
    base_url: str = GLM_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    thinking: GLMThinkingMode | None = None
    reasoning_effort: GLMReasoningEffort | None = None
    max_tokens: int | None = None
    user_id: str | None = None
    name: Literal["glm"] = field(default="glm", init=False)

    def build(self, api_key: str) -> GLMConfig:
        """Create the runtime provider config after credential resolution."""
        return GLMConfig(
            api_key=api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            thinking=self.thinking,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
            user_id=self.user_id,
        )


@dataclass(frozen=True, slots=True)
class TimiccSettings:
    """Non-secret TiMi Responses API settings loaded from YAML."""

    model: str
    api_key_env: str = TIMICC_DEFAULT_API_KEY_ENV
    base_url: str = TIMICC_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    reasoning_effort: TimiccReasoningEffort | None = None
    reasoning_summary: TimiccReasoningSummary | None = None
    max_output_tokens: int | None = None
    name: Literal["timicc"] = field(default="timicc", init=False)

    def build(self, api_key: str) -> TimiccResponsesConfig:
        return TimiccResponsesConfig(
            api_key=api_key,
            model=self.model,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            reasoning_effort=self.reasoning_effort,
            reasoning_summary=self.reasoning_summary,
            max_output_tokens=self.max_output_tokens,
        )


@dataclass(frozen=True, slots=True)
class ProviderResilienceSettings:
    """Non-secret retry/backoff limits parsed at the application boundary."""

    enabled: bool = False
    max_attempts_per_provider: int = 2
    max_total_attempts: int = 4
    max_elapsed_seconds: float = 30.0
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ConfigurationError("resilience.enabled must be a boolean")
        for name, value in (
            ("max_attempts_per_provider", self.max_attempts_per_provider),
            ("max_total_attempts", self.max_total_attempts),
        ):
            if type(value) is not int or value <= 0:
                raise ConfigurationError(f"resilience.{name} must be a positive integer")
        for name, value, allow_zero in (
            ("max_elapsed_seconds", self.max_elapsed_seconds, False),
            ("base_delay_seconds", self.base_delay_seconds, True),
            ("max_delay_seconds", self.max_delay_seconds, True),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ConfigurationError(
                    f"resilience.{name} must be a finite {qualifier} number"
                )
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ConfigurationError(
                "resilience.max_delay_seconds must be at least base_delay_seconds"
            )


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


@dataclass(frozen=True, slots=True)
class AppConfigSpec:
    """Validated, non-secret configuration parsed from one YAML file."""

    provider: ProviderSettings
    config_path: Path
    resilience: ProviderResilienceSettings = ProviderResilienceSettings()
    bash: DockerBashSettings | None = None
    version: Literal[1] = 1


@dataclass(frozen=True, slots=True)
class CredentialDiagnostic:
    """Safe credential metadata suitable for user-facing diagnostics."""

    variable: str
    source: CredentialSource
    masked_value: str
    source_path: Path | None = None


@dataclass(frozen=True, slots=True)
class LoadedAppConfig:
    """Resolved application configuration passed into assembly code."""

    spec: AppConfigSpec
    provider_config: ProviderRuntimeConfig = field(repr=False)
    credential: CredentialDiagnostic

    @property
    def provider_name(self) -> ProviderName:
        return self.spec.provider.name


@dataclass(frozen=True, slots=True)
class SecretWriteResult:
    """Non-secret result of an explicit dotenv update."""

    path: Path
    variable: str
    replaced: bool


def parse_app_config(
    config_path: str | Path, *, provider_catalog: ProviderCatalog | None = None
) -> AppConfigSpec:
    """Parse and validate one explicit YAML configuration path."""
    path = Path(config_path).resolve()
    root = _load_yaml_mapping(path)
    _reject_unknown_keys(root, _ROOT_KEYS, context="configuration root")

    version = root.get("version")
    if version != 1 or isinstance(version, bool):
        raise ConfigurationError("configuration version must be the integer 1")
    provider_raw = root.get("provider")
    if not isinstance(provider_raw, dict):
        raise ConfigurationError("configuration provider must be a mapping")
    provider = _parse_provider(provider_raw, provider_catalog)
    resilience = _parse_resilience(root.get("resilience"))
    bash = _parse_bash(root.get("bash"), config_path=path)

    return AppConfigSpec(
        provider=provider,
        config_path=path,
        resilience=resilience,
        bash=bash,
    )


def load_app_config(
    config_path: str | Path,
    *,
    env_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    api_key_override: str | None = None,
    provider_catalog: ProviderCatalog | None = None,
) -> LoadedAppConfig:
    """Load YAML once and resolve its API key without mutating global state.

    Credential precedence is explicit override, process environment, then the
    explicitly selected dotenv file. The loader never searches parent folders.
    """
    spec = parse_app_config(config_path, provider_catalog=provider_catalog)
    selected_env_path = Path(env_path).resolve() if env_path is not None else None
    key, diagnostic = _resolve_api_key(
        spec.provider.api_key_env,
        env_path=selected_env_path,
        environ=os.environ if environ is None else environ,
        api_key_override=api_key_override,
    )
    try:
        provider_config = spec.provider.build(key)
    except ConfigurationError:
        raise
    except Exception as error:
        raise ConfigurationError("provider configuration resolution failed") from error
    if (
        not isinstance(getattr(provider_config, "model", None), str)
        or not provider_config.model.strip()
    ):
        raise ConfigurationError("provider config must expose a non-empty model")
    return LoadedAppConfig(
        spec=spec,
        provider_config=provider_config,
        credential=diagnostic,
    )


def format_config_diagnostics(config: LoadedAppConfig) -> str:
    """Render user-facing configuration diagnostics without exposing secrets."""
    credential = config.credential
    if credential.source == "explicit":
        source = "explicit override"
    elif credential.source == "process_env":
        source = "process environment"
    else:
        source = f"dotenv {credential.source_path}"
    bash = config.spec.bash
    bash_diagnostic = "bash: disabled"
    if bash is not None:
        network = "enabled" if bash.network_enabled else "disabled"
        bash_diagnostic = (
            f"bash: docker image={bash.image} mode={bash.mode} "
            f"network={network} workspace={bash.workspace_root}"
        )
    return "\n".join(
        (
            f"provider: {config.provider_name}",
            f"model: {config.provider_config.model}",
            "resilience: "
            + (
                "enabled "
                f"(provider_attempts={config.spec.resilience.max_attempts_per_provider}, "
                f"total_attempts={config.spec.resilience.max_total_attempts})"
                if config.spec.resilience.enabled
                else "disabled"
            ),
            f"config: {config.spec.config_path}",
            bash_diagnostic,
            f"credential: {credential.variable} from {source} "
            f"({credential.masked_value})",
        )
    )


def mask_secret(value: str) -> str:
    """Return a stable masked value that reveals at most the last four chars."""
    if len(value) <= 8:
        return "********"
    return f"********{value[-4:]}"


def write_env_secret(
    env_path: str | Path,
    variable: str,
    value: str,
    *,
    overwrite: bool = False,
) -> SecretWriteResult:
    """Atomically write one secret to an explicitly selected dotenv file.

    Existing unrelated lines are preserved. A configured value is not replaced
    unless ``overwrite`` is true. The secret is never returned or formatted.
    """
    _validate_env_name(variable)
    _validate_secret(value)
    path = Path(env_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigurationError(f"cannot read dotenv file: {path}") from error
    parsed = _parse_dotenv(existing, source=path) if existing else {}
    replaced = variable in parsed
    if replaced and parsed[variable] and not overwrite:
        raise ConfigurationError(
            f"{variable} is already configured in {path}; use overwrite=True "
            "to replace it"
        )

    replacement = f'{variable}="{_escape_dotenv_value(value)}"'
    lines = existing.splitlines()
    matching_indexes = [
        index
        for index, line in enumerate(lines)
        if _dotenv_assignment_name(line) == variable
    ]
    if len(matching_indexes) > 1:
        raise ConfigurationError(f"duplicate dotenv variable {variable} in {path}")
    if matching_indexes:
        lines[matching_indexes[0]] = replacement
    else:
        lines.append(replacement)
    content = "\n".join(lines) + "\n"
    _atomic_write_private(path, content)
    return SecretWriteResult(path=path, variable=variable, replaced=replaced)


def _parse_provider(
    values: dict[object, object], catalog: ProviderCatalog | None
) -> ProviderSettings:
    from .provider_catalog import default_provider_catalog

    if any(not isinstance(key, str) for key in values):
        raise ConfigurationError("provider settings keys must be strings")
    name = values.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigurationError("provider.name must be a non-empty string")
    selected = catalog if catalog is not None else default_provider_catalog()
    return selected.parse_settings(name, cast(Mapping[str, object], values))


def _validated_builtin_settings(settings: SettingsT) -> SettingsT:
    settings.build("configuration-validation-placeholder")
    return settings


def _parse_deepseek_settings(values: Mapping[str, object]) -> DeepSeekSettings:
    options: dict[object, object] = dict(values)
    _reject_unknown_keys(options, _DEEPSEEK_KEYS, context="DeepSeek provider")
    return DeepSeekSettings(
        api_key_env=_env_name(
            options,
            "api_key_env",
            DEEPSEEK_DEFAULT_API_KEY_ENV,
        ),
        model=_string(options, "model", DEEPSEEK_DEFAULT_MODEL),
        base_url=_string(options, "base_url", DEEPSEEK_DEFAULT_BASE_URL),
        timeout_seconds=_positive_number(
            options,
            "timeout_seconds",
            DEFAULT_TIMEOUT_SECONDS,
        ),
        thinking=cast(
            ThinkingMode,
            _string(options, "thinking", DEEPSEEK_DEFAULT_THINKING),
        ),
        reasoning_effort=cast(
            ReasoningEffort | None,
            _optional_string(options, "reasoning_effort"),
        ),
        max_tokens=_optional_positive_int(options, "max_tokens"),
        user_id=_optional_string(options, "user_id"),
    )


def _parse_kimi_settings(values: Mapping[str, object]) -> KimiSettings:
    options: dict[object, object] = dict(values)
    _reject_unknown_keys(options, _KIMI_KEYS, context="Kimi provider")
    return KimiSettings(
        api_key_env=_env_name(options, "api_key_env", KIMI_DEFAULT_API_KEY_ENV),
        model=_string(options, "model", KIMI_DEFAULT_MODEL),
        base_url=_string(options, "base_url", KIMI_DEFAULT_BASE_URL),
        timeout_seconds=_positive_number(
            options,
            "timeout_seconds",
            DEFAULT_TIMEOUT_SECONDS,
        ),
        thinking=cast(
            KimiThinkingMode | None,
            _optional_string(options, "thinking"),
        ),
        thinking_keep=cast(
            KimiThinkingKeep | None,
            _optional_string(options, "thinking_keep"),
        ),
        reasoning_effort=cast(
            KimiReasoningEffort | None,
            _optional_string(options, "reasoning_effort"),
        ),
        max_tokens=_optional_positive_int(options, "max_tokens"),
    )


def _parse_glm_settings(values: Mapping[str, object]) -> GLMSettings:
    options: dict[object, object] = dict(values)
    _reject_unknown_keys(options, _GLM_KEYS, context="GLM provider")
    return GLMSettings(
        api_key_env=_env_name(options, "api_key_env", GLM_DEFAULT_API_KEY_ENV),
        model=_string(options, "model", GLM_DEFAULT_MODEL),
        base_url=_string(options, "base_url", GLM_DEFAULT_BASE_URL),
        timeout_seconds=_positive_number(
            options,
            "timeout_seconds",
            DEFAULT_TIMEOUT_SECONDS,
        ),
        thinking=cast(
            GLMThinkingMode | None,
            _optional_string(options, "thinking"),
        ),
        reasoning_effort=cast(
            GLMReasoningEffort | None,
            _optional_string(options, "reasoning_effort"),
        ),
        max_tokens=_optional_positive_int(options, "max_tokens"),
        user_id=_optional_string(options, "user_id"),
    )


def _parse_timicc_settings(values: Mapping[str, object]) -> TimiccSettings:
    options: dict[object, object] = dict(values)
    _reject_unknown_keys(options, _TIMICC_KEYS, context="TiMi provider")
    return TimiccSettings(
        api_key_env=_env_name(
            options,
            "api_key_env",
            TIMICC_DEFAULT_API_KEY_ENV,
        ),
        model=_required_string(options, "model"),
        base_url=_string(options, "base_url", TIMICC_DEFAULT_BASE_URL),
        timeout_seconds=_positive_number(
            options,
            "timeout_seconds",
            DEFAULT_TIMEOUT_SECONDS,
        ),
        reasoning_effort=cast(
            TimiccReasoningEffort | None,
            _optional_string(options, "reasoning_effort"),
        ),
        reasoning_summary=cast(
            TimiccReasoningSummary | None,
            _optional_string(options, "reasoning_summary"),
        ),
        max_output_tokens=_optional_positive_int(
            options,
            "max_output_tokens",
        ),
    )


def _parse_resilience(value: object) -> ProviderResilienceSettings:
    if value is None:
        return ProviderResilienceSettings()
    if not isinstance(value, dict):
        raise ConfigurationError("configuration resilience must be a mapping")
    _reject_unknown_keys(value, _RESILIENCE_KEYS, context="provider resilience")
    return ProviderResilienceSettings(
        enabled=_boolean(value, "enabled", False),
        max_attempts_per_provider=_positive_int(
            value,
            "max_attempts_per_provider",
            2,
        ),
        max_total_attempts=_positive_int(value, "max_total_attempts", 4),
        max_elapsed_seconds=_resilience_positive_number(
            value,
            "max_elapsed_seconds",
            30.0,
        ),
        base_delay_seconds=_non_negative_number(
            value,
            "base_delay_seconds",
            0.25,
        ),
        max_delay_seconds=_non_negative_number(
            value,
            "max_delay_seconds",
            8.0,
        ),
    )


def _parse_bash(
    value: object,
    *,
    config_path: Path,
) -> DockerBashSettings | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigurationError("configuration bash must be a mapping")
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


def _resolve_api_key(
    variable: str,
    *,
    env_path: Path | None,
    environ: Mapping[str, str],
    api_key_override: str | None,
) -> tuple[str, CredentialDiagnostic]:
    if api_key_override is not None:
        _validate_secret(api_key_override)
        return api_key_override, CredentialDiagnostic(
            variable=variable,
            source="explicit",
            masked_value=mask_secret(api_key_override),
        )

    process_value = environ.get(variable)
    if process_value:
        _validate_secret(process_value)
        return process_value, CredentialDiagnostic(
            variable=variable,
            source="process_env",
            masked_value=mask_secret(process_value),
        )

    dotenv_values: dict[str, str] = {}
    if env_path is not None and env_path.exists():
        try:
            dotenv_values = _parse_dotenv(
                env_path.read_text(encoding="utf-8"),
                source=env_path,
            )
        except OSError as error:
            raise ConfigurationError(f"cannot read dotenv file: {env_path}") from error
    dotenv_value = dotenv_values.get(variable)
    if dotenv_value:
        _validate_secret(dotenv_value)
        return dotenv_value, CredentialDiagnostic(
            variable=variable,
            source="dotenv",
            masked_value=mask_secret(dotenv_value),
            source_path=env_path,
        )

    locations = "the process environment"
    if env_path is not None:
        locations += f" or {env_path}"
    raise ConfigurationError(
        f"missing API key: set {variable} in {locations}; "
        "the loader does not search parent directories"
    )


def _load_yaml_mapping(path: Path) -> dict[object, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(f"cannot read configuration file: {path}") from error
    try:
        loaded = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = ""
        if mark is not None:
            location = f" at line {mark.line + 1}, column {mark.column + 1}"
        raise ConfigurationError(f"invalid YAML in {path}{location}") from error
    if not isinstance(loaded, dict):
        raise ConfigurationError("configuration root must be a mapping")
    return loaded


def _parse_dotenv(text: str, *, source: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOTENV_ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ConfigurationError(
                f"invalid dotenv assignment in {source} at line {line_number}"
            )
        name = match.group("name")
        if name in values:
            raise ConfigurationError(
                f"duplicate dotenv variable {name} in {source} at line {line_number}"
            )
        values[name] = _parse_dotenv_value(
            match.group("value"),
            source=source,
            line_number=line_number,
        )
    return values


def _parse_dotenv_value(raw: str, *, source: Path, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        escaped = False
        result: list[str] = []
        closing_index: int | None = None
        for index, character in enumerate(value[1:], start=1):
            if quote == '"' and escaped:
                result.append(
                    {"n": "\n", "r": "\r", "t": "\t"}.get(character, character)
                )
                escaped = False
                continue
            if quote == '"' and character == "\\":
                escaped = True
                continue
            if character == quote:
                closing_index = index
                break
            result.append(character)
        if escaped or closing_index is None:
            raise ConfigurationError(
                f"unterminated dotenv quote in {source} at line {line_number}"
            )
        remainder = value[closing_index + 1 :].strip()
        if remainder and not remainder.startswith("#"):
            raise ConfigurationError(
                f"unexpected dotenv text in {source} at line {line_number}"
            )
        return "".join(result)
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def _dotenv_assignment_name(line: str) -> str | None:
    match = _DOTENV_ASSIGNMENT.fullmatch(line)
    return match.group("name") if match is not None else None


def _escape_dotenv_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _atomic_write_private(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except OSError as error:
        raise ConfigurationError(f"cannot write dotenv file: {path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


def _reject_unknown_keys(
    values: Mapping[object, object],
    allowed: set[str],
    *,
    context: str,
) -> None:
    invalid = sorted(str(key) for key in values if key not in allowed)
    if invalid:
        raise ConfigurationError(
            f"unknown {context} field(s): {', '.join(invalid)}"
        )


def _string(values: Mapping[object, object], key: str, default: str) -> str:
    value = values.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"provider.{key} must be a non-empty string")
    return value


def _required_string(values: Mapping[object, object], key: str) -> str:
    if key not in values:
        raise ConfigurationError(f"provider.{key} is required")
    return _string(values, key, "")


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


def _optional_string(values: Mapping[object, object], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"provider.{key} must be a non-empty string or null")
    return value


def _positive_number(
    values: Mapping[object, object],
    key: str,
    default: float,
) -> float:
    value = values.get(key, default)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ConfigurationError(f"provider.{key} must be greater than zero")
    return float(value)


def _resilience_positive_number(
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
        raise ConfigurationError(f"resilience.{key} must be greater than zero")
    return float(value)


def _non_negative_number(
    values: Mapping[object, object],
    key: str,
    default: float,
) -> float:
    value = values.get(key, default)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ConfigurationError(
            f"resilience.{key} must be a non-negative number"
        )
    return float(value)


def _positive_int(
    values: Mapping[object, object],
    key: str,
    default: int,
) -> int:
    value = values.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"resilience.{key} must be a positive integer")
    return value


def _boolean(
    values: Mapping[object, object],
    key: str,
    default: bool,
) -> bool:
    value = values.get(key, default)
    if type(value) is not bool:
        raise ConfigurationError(f"resilience.{key} must be a boolean")
    return value


def _optional_positive_int(
    values: Mapping[object, object],
    key: str,
) -> int | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"provider.{key} must be a positive integer or null")
    return value


def _env_name(values: Mapping[object, object], key: str, default: str) -> str:
    value = _string(values, key, default)
    _validate_env_name(value)
    return value


def _validate_env_name(value: str) -> None:
    if _ENV_NAME.fullmatch(value) is None:
        raise ConfigurationError(
            "API key environment variable must match [A-Za-z_][A-Za-z0-9_]*"
        )


def _validate_secret(value: str) -> None:
    if not value or not value.strip():
        raise ConfigurationError("API key must not be empty")
    if value != value.strip() or any(character in value for character in "\r\n\x00"):
        raise ConfigurationError(
            "API key must not contain surrounding whitespace, newlines, or NUL"
        )
