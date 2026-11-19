from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from pi_agent_py import ConfigurationError
from pi_agent_py.application.configuration import (
    DeepSeekSettings,
    DockerBashSettings,
    GLMSettings,
    KimiSettings,
    ProviderResilienceSettings,
    TimiccSettings,
    format_config_diagnostics,
    load_app_config,
    parse_app_config,
    write_env_secret,
)
from pi_agent_py.providers import (
    DeepSeekConfig,
    GLMConfig,
    KimiConfig,
    TimiccResponsesConfig,
)


class ConfigurationTests(unittest.TestCase):
    def test_parses_docker_bash_settings_relative_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config_path = _write(
                root / "agent.yaml",
                """
                version: 1
                provider:
                  name: deepseek
                bash:
                  provider: docker
                  workspace_root: workspace
                  image: local/pi-agent:test
                  docker_executable: docker-test
                  mode: read-only
                  default_timeout_seconds: 30
                  max_timeout_seconds: 90
                  max_output_bytes: 4096
                  network_enabled: false
                """,
            )

            spec = parse_app_config(config_path)
            loaded = load_app_config(
                config_path,
                environ={"DEEPSEEK_API_KEY": "test-secret"},
            )

            self.assertEqual(
                spec.bash,
                DockerBashSettings(
                    workspace_root=workspace.resolve(),
                    image="local/pi-agent:test",
                    docker_executable="docker-test",
                    mode="read-only",
                    default_timeout_seconds=30,
                    max_timeout_seconds=90,
                    max_output_bytes=4096,
                    network_enabled=False,
                ),
            )
            diagnostics = format_config_diagnostics(loaded)
            self.assertIn("bash: docker", diagnostics)
            self.assertIn("mode=read-only", diagnostics)
            self.assertIn("network=disabled", diagnostics)
            self.assertNotIn("test-secret", diagnostics)

    def test_bash_defaults_disabled_and_invalid_settings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(parse_app_config(_kimi_yaml(root)).bash)

            invalid_cases = (
                ("[]", "bash must be a mapping"),
                ("provider: host\nimage: local/test", "must be 'docker'"),
                ("provider: docker", "bash.image is required"),
                (
                    "provider: docker\nimage: local/test\nmode: danger-full-access",
                    "bash.mode",
                ),
                (
                    "provider: docker\nimage: local/test\n"
                    "default_timeout_seconds: 20\nmax_timeout_seconds: 10",
                    "must not exceed",
                ),
                (
                    "provider: docker\nimage: local/test\nmax_output_bytes: 0",
                    "positive integer",
                ),
                (
                    "provider: docker\nimage: local/test\nnetwork_enabled: 1",
                    "must be a boolean",
                ),
                (
                    "provider: docker\nimage: local/test\nsurprise: true",
                    "unknown Bash field",
                ),
            )
            for index, (body, message) in enumerate(invalid_cases):
                with self.subTest(body=body):
                    path = _write(
                        root / f"bash-invalid-{index}.yaml",
                        "version: 1\n"
                        "provider:\n"
                        "  name: deepseek\n"
                        "bash:\n"
                        + textwrap.indent(body, "  "),
                    )
                    with self.assertRaisesRegex(ConfigurationError, message):
                        parse_app_config(path)

    def test_yaml_defaults_match_direct_provider_config_defaults(self) -> None:
        cases = (
            (
                "deepseek",
                "DEEPSEEK_API_KEY",
                DeepSeekConfig(api_key="test-key"),
            ),
            (
                "kimi",
                "MOONSHOT_API_KEY",
                KimiConfig(api_key="test-key"),
            ),
            (
                "glm",
                "ZAI_API_KEY",
                GLMConfig(api_key="test-key"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for provider_name, api_key_env, direct_config in cases:
                with self.subTest(provider=provider_name):
                    config_path = _write(
                        root / f"{provider_name}.yaml",
                        f"version: 1\nprovider:\n  name: {provider_name}\n",
                    )

                    spec = parse_app_config(config_path)
                    loaded = load_app_config(
                        config_path,
                        environ={api_key_env: "test-key"},
                    )

                    self.assertEqual(spec.provider.api_key_env, api_key_env)
                    self.assertEqual(loaded.provider_config, direct_config)

    def test_provider_configs_share_common_validation_contract(self) -> None:
        cases = (
            ({"api_key": "  "}, "API key must not be empty"),
            ({"api_key": "test-key", "model": "  "}, "model must not be empty"),
            (
                {"api_key": "test-key", "base_url": "relative/path"},
                "base_url must be an absolute HTTP",
            ),
            (
                {"api_key": "test-key", "timeout_seconds": 0},
                "timeout_seconds must be greater than zero",
            ),
            (
                {"api_key": "test-key", "max_tokens": 0},
                "max_tokens must be greater than zero",
            ),
        )
        for config_type in (DeepSeekConfig, KimiConfig, GLMConfig):
            for kwargs, message in cases:
                with self.subTest(config=config_type.__name__, kwargs=kwargs):
                    with self.assertRaisesRegex(ConfigurationError, message):
                        config_type(**kwargs)

    def test_loads_typed_kimi_config_from_explicit_yaml_and_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write(
                root / "agent.yaml",
                """
                version: 1
                provider:
                  name: kimi
                  api_key_env: TEST_MOONSHOT_KEY
                  model: kimi-k2.6
                  thinking: enabled
                  thinking_keep: all
                  max_tokens: 16000
                """,
            )
            env_path = _write(
                root / ".env",
                'TEST_MOONSHOT_KEY="dotenv-secret-1234"\n',
            )

            loaded = load_app_config(
                config_path,
                env_path=env_path,
                environ={},
            )

            self.assertIsInstance(loaded.spec.provider, KimiSettings)
            self.assertIsInstance(loaded.provider_config, KimiConfig)
            assert isinstance(loaded.provider_config, KimiConfig)
            self.assertEqual(loaded.provider_config.api_key, "dotenv-secret-1234")
            self.assertEqual(loaded.provider_config.thinking, "enabled")
            self.assertEqual(loaded.provider_config.thinking_keep, "all")
            self.assertEqual(loaded.provider_config.max_tokens, 16000)
            self.assertEqual(loaded.credential.source, "dotenv")
            self.assertEqual(loaded.credential.masked_value, "********1234")
            diagnostics = format_config_diagnostics(loaded)
            self.assertIn("provider: kimi", diagnostics)
            self.assertIn("TEST_MOONSHOT_KEY from dotenv", diagnostics)
            self.assertNotIn("dotenv-secret-1234", diagnostics)
            self.assertNotIn("dotenv-secret-1234", repr(loaded))

    def test_process_environment_overrides_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _kimi_yaml(root)
            env_path = _write(root / ".env", "MOONSHOT_API_KEY=dotenv-key\n")

            loaded = load_app_config(
                config_path,
                env_path=env_path,
                environ={"MOONSHOT_API_KEY": "process-key"},
            )

            self.assertEqual(loaded.provider_config.api_key, "process-key")
            self.assertEqual(loaded.credential.source, "process_env")
            self.assertIsNone(loaded.credential.source_path)

    def test_explicit_override_has_highest_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = load_app_config(
                _kimi_yaml(root),
                env_path=_write(root / ".env", "MOONSHOT_API_KEY=dotenv-key\n"),
                environ={"MOONSHOT_API_KEY": "process-key"},
                api_key_override="explicit-key",
            )

            self.assertEqual(loaded.provider_config.api_key, "explicit-key")
            self.assertEqual(loaded.credential.source, "explicit")

    def test_builds_typed_deepseek_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write(
                root / "agent.yaml",
                """
                version: 1
                provider:
                  name: deepseek
                  api_key_env: CUSTOM_DEEPSEEK_KEY
                  model: deepseek-flash
                  thinking: enabled
                  reasoning_effort: high
                  timeout_seconds: 30
                  user_id: learner_1
                """,
            )

            loaded = load_app_config(
                config_path,
                environ={"CUSTOM_DEEPSEEK_KEY": "deepseek-secret"},
            )

            self.assertIsInstance(loaded.spec.provider, DeepSeekSettings)
            self.assertIsInstance(loaded.provider_config, DeepSeekConfig)
            assert isinstance(loaded.provider_config, DeepSeekConfig)
            self.assertEqual(loaded.provider_config.reasoning_effort, "high")
            self.assertEqual(loaded.provider_config.timeout_seconds, 30)
            self.assertEqual(loaded.provider_config.user_id, "learner_1")

    def test_builds_typed_glm_config_and_masks_zai_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write(
                root / "agent.yaml",
                """
                version: 1
                provider:
                  name: glm
                  api_key_env: TEST_ZAI_KEY
                  model: glm-5.2
                  thinking: enabled
                  reasoning_effort: medium
                  max_tokens: 4096
                  user_id: learner-01
                """,
            )

            loaded = load_app_config(
                config_path,
                environ={"TEST_ZAI_KEY": "glm-secret-5678"},
            )

            self.assertIsInstance(loaded.spec.provider, GLMSettings)
            self.assertIsInstance(loaded.provider_config, GLMConfig)
            assert isinstance(loaded.provider_config, GLMConfig)
            self.assertEqual(loaded.provider_config.reasoning_effort, "medium")
            self.assertEqual(loaded.provider_config.max_tokens, 4096)
            self.assertEqual(loaded.provider_config.user_id, "learner-01")
            diagnostics = format_config_diagnostics(loaded)
            self.assertIn("provider: glm", diagnostics)
            self.assertIn("********5678", diagnostics)
            self.assertNotIn("glm-secret-5678", diagnostics)
            self.assertNotIn("glm-secret-5678", repr(loaded))

    def test_builds_typed_timicc_responses_config_with_required_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write(
                root / "agent.yaml",
                """
                version: 1
                provider:
                  name: timicc
                  api_key_env: TEST_TIMICC_KEY
                  model: gpt-6-astra
                  reasoning_effort: high
                  reasoning_summary: auto
                  max_output_tokens: 4096
                """,
            )

            loaded = load_app_config(
                config_path,
                environ={"TEST_TIMICC_KEY": "timicc-secret-9012"},
            )

            self.assertIsInstance(loaded.spec.provider, TimiccSettings)
            self.assertIsInstance(loaded.provider_config, TimiccResponsesConfig)
            assert isinstance(loaded.provider_config, TimiccResponsesConfig)
            self.assertEqual(loaded.provider_config.model, "gpt-6-astra")
            self.assertEqual(loaded.provider_config.reasoning_effort, "high")
            self.assertEqual(loaded.provider_config.reasoning_summary, "auto")
            self.assertEqual(loaded.provider_config.max_output_tokens, 4096)
            diagnostics = format_config_diagnostics(loaded)
            self.assertIn("provider: timicc", diagnostics)
            self.assertIn("********9012", diagnostics)
            self.assertNotIn("timicc-secret-9012", repr(loaded))

    def test_never_searches_parent_directories_for_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write(root / ".env", "MOONSHOT_API_KEY=parent-secret\n")
            nested = root / "nested"
            nested.mkdir()
            config_path = _kimi_yaml(nested)

            with self.assertRaisesRegex(
                ConfigurationError,
                "does not search parent directories",
            ) as raised:
                load_app_config(config_path, environ={})

            self.assertNotIn("parent-secret", str(raised.exception))

    def test_rejects_inline_secret_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inline = _write(
                root / "inline.yaml",
                """
                version: 1
                provider:
                  name: kimi
                  api_key: do-not-store-here
                """,
            )
            with self.assertRaisesRegex(ConfigurationError, "inline provider.api_key"):
                parse_app_config(inline)

            unknown = _write(
                root / "unknown.yaml",
                """
                version: 1
                provider:
                  name: kimi
                  surprise: true
                """,
            )
            with self.assertRaisesRegex(ConfigurationError, "surprise"):
                parse_app_config(unknown)

            duplicate = _write(
                root / "duplicate.yaml",
                """
                version: 1
                provider:
                  name: kimi
                  name: deepseek
                """,
            )
            with self.assertRaisesRegex(ConfigurationError, "invalid YAML"):
                parse_app_config(duplicate)

    def test_rejects_invalid_yaml_types_and_provider_combinations(self) -> None:
        cases = (
            (
                "version: true\nprovider:\n  name: kimi\n",
                "version must be the integer 1",
            ),
            (
                "version: 1\nprovider:\n  name: kimi\n  timeout_seconds: fast\n",
                "timeout_seconds must be greater than zero",
            ),
            (
                "version: 1\nprovider:\n  name: kimi\n  model: kimi-k3\n  thinking: enabled\n",
                "does not accept the thinking parameter",
            ),
            (
                "version: 1\nprovider:\n  name: unknown\n",
                "provider.name",
            ),
            (
                "version: 1\nprovider:\n  name: glm\n  thinking: disabled\n",
                "cannot be disabled",
            ),
            (
                "version: 1\nprovider:\n  name: timicc\n",
                "provider.model is required",
            ),
            (
                "version: 1\nprovider:\n  name: timicc\n  model: gpt-5\n  max_tokens: 10\n",
                "max_tokens",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (content, message) in enumerate(cases):
                with self.subTest(index=index):
                    path = _write(root / f"case-{index}.yaml", content)
                    with self.assertRaisesRegex(ConfigurationError, message):
                        parse_app_config(path)

    def test_missing_key_reports_checked_locations_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"

            with self.assertRaisesRegex(
                ConfigurationError,
                "MOONSHOT_API_KEY",
            ) as raised:
                load_app_config(
                    _kimi_yaml(root),
                    env_path=env_path,
                    environ={},
                )

            self.assertIn(str(env_path.resolve()), str(raised.exception))

    def test_explicit_writer_preserves_file_and_requires_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = _write(root / ".env", "# local secrets\nOTHER=value\n")

            first = write_env_secret(
                env_path,
                "MOONSHOT_API_KEY",
                'secret-with-quote-"-and-backslash-\\-9876',
            )

            self.assertFalse(first.replaced)
            self.assertNotIn("secret-with", repr(first))
            content = env_path.read_text(encoding="utf-8")
            self.assertIn("# local secrets", content)
            self.assertIn("OTHER=value", content)
            loaded = load_app_config(
                _kimi_yaml(root),
                env_path=env_path,
                environ={},
            )
            self.assertEqual(
                loaded.provider_config.api_key,
                'secret-with-quote-"-and-backslash-\\-9876',
            )

            with self.assertRaisesRegex(ConfigurationError, "overwrite=True"):
                write_env_secret(env_path, "MOONSHOT_API_KEY", "replacement")
            second = write_env_secret(
                env_path,
                "MOONSHOT_API_KEY",
                "replacement-4321",
                overwrite=True,
            )
            self.assertTrue(second.replaced)
            replaced = load_app_config(
                _kimi_yaml(root),
                env_path=env_path,
                environ={},
            )
            self.assertEqual(replaced.provider_config.api_key, "replacement-4321")

    def test_duplicate_dotenv_variables_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = _write(
                root / ".env",
                "MOONSHOT_API_KEY=one\nMOONSHOT_API_KEY=two\n",
            )

            with self.assertRaisesRegex(ConfigurationError, "duplicate dotenv"):
                load_app_config(
                    _kimi_yaml(root),
                    env_path=env_path,
                    environ={},
                )

    def test_parses_optional_provider_resilience_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = _write(
                root / "agent.yaml",
                """
                version: 1
                provider:
                  name: deepseek
                resilience:
                  enabled: true
                  max_attempts_per_provider: 3
                  max_total_attempts: 5
                  max_elapsed_seconds: 20
                  base_delay_seconds: 0.1
                  max_delay_seconds: 3
                """,
            )

            spec = parse_app_config(config_path)
            loaded = load_app_config(
                config_path,
                environ={"DEEPSEEK_API_KEY": "secret-value"},
            )

            self.assertEqual(
                spec.resilience,
                ProviderResilienceSettings(
                    enabled=True,
                    max_attempts_per_provider=3,
                    max_total_attempts=5,
                    max_elapsed_seconds=20,
                    base_delay_seconds=0.1,
                    max_delay_seconds=3,
                ),
            )
            diagnostics = format_config_diagnostics(loaded)
            self.assertIn("resilience: enabled", diagnostics)
            self.assertNotIn("secret-value", diagnostics)

    def test_resilience_defaults_disabled_and_invalid_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = parse_app_config(_kimi_yaml(root))
            self.assertEqual(defaults.resilience, ProviderResilienceSettings())

            invalid_cases = (
                ("enabled: 1", "enabled must be a boolean"),
                ("max_attempts_per_provider: 0", "positive integer"),
                (
                    "base_delay_seconds: 2\nmax_delay_seconds: 1",
                    "must be at least",
                ),
                ("unknown: true", "unknown provider resilience field"),
            )
            for index, (body, message) in enumerate(invalid_cases):
                with self.subTest(body=body):
                    path = _write(
                        root / f"invalid-{index}.yaml",
                        "version: 1\n"
                        "provider:\n"
                        "  name: deepseek\n"
                        "resilience:\n"
                        + textwrap.indent(body, "  "),
                    )
                    with self.assertRaisesRegex(ConfigurationError, message):
                        parse_app_config(path)


def _kimi_yaml(root: Path) -> Path:
    return _write(
        root / "agent.yaml",
        """
        version: 1
        provider:
          name: kimi
          api_key_env: MOONSHOT_API_KEY
        """,
    )


def _write(path: Path, content: str) -> Path:
    normalized = textwrap.dedent(content).strip()
    path.write_text(normalized + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
