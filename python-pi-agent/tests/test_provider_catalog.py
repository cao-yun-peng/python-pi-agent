from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from downstream_provider.echo import (
    EchoConfig,
    EchoProvider,
    EchoSettings,
    echo_registration,
    make_echo_provider,
    parse_echo_settings,
)
from pi_agent_py.api import (
    AgentEvent,
    AgentRuntimeBuilder,
    CallbackEventSink,
    ConfigurationError,
    Finish,
    ModelProvider,
    ProviderCatalog,
    ProviderRegistration,
    ProviderRuntimeConfig,
    ProviderSettings,
    RuntimeBuildError,
    default_provider_catalog,
    format_config_diagnostics,
    load_app_config,
    parse_app_config,
)
from pi_agent_py.testing import FakeProvider


def _yaml(root: Path, body: str = "") -> Path:
    path = root / "agent.yaml"
    path.write_text(
        "version: 1\nprovider:\n  name: echo\n  model: custom-v2\n" + body,
        encoding="utf-8",
    )
    return path


class CatalogConfigurationTests(unittest.TestCase):
    def test_external_parser_builds_typed_settings_before_credentials(self) -> None:
        calls: list[str] = []

        def parse(raw):
            calls.append("parse")
            return parse_echo_settings(raw)

        def factory(config: ProviderRuntimeConfig) -> ModelProvider:
            self.fail("configuration parsing constructed a Provider")

        catalog = ProviderCatalog(
            (ProviderRegistration("echo", factory, parse_settings=parse),)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = _yaml(Path(directory), "  prefix: 'reply '\n")
            spec = parse_app_config(path, provider_catalog=catalog)
            self.assertEqual(spec.provider, EchoSettings("custom-v2", prefix="reply "))
            self.assertEqual(calls, ["parse"])
            loaded = load_app_config(
                path,
                provider_catalog=catalog,
                environ={"ECHO_API_KEY": "test-value-1234"},
            )
        self.assertIsInstance(loaded.provider_config, EchoConfig)
        self.assertEqual(loaded.provider_name, "echo")
        self.assertEqual(loaded.provider_config.api_key, "test-value-1234")
        self.assertEqual(loaded.credential.source, "process_env")
        self.assertNotIn("test-value-1234", repr(loaded))
        diagnostics = format_config_diagnostics(loaded)
        self.assertIn("provider: echo", diagnostics)
        self.assertIn("model: custom-v2", diagnostics)
        self.assertNotIn("test-value-1234", diagnostics)
        self.assertEqual(calls, ["parse", "parse"])

    def test_catalog_extension_is_immutable_and_checks_duplicate_names(self) -> None:
        original = default_provider_catalog()
        extended = original.with_registration(echo_registration())
        self.assertEqual(original.names, ("deepseek", "kimi", "glm", "timicc"))
        self.assertIn("echo", extended.names)
        with self.assertRaises(ValueError):
            extended.with_registration(echo_registration())
        with tempfile.TemporaryDirectory() as directory:
            path = _yaml(Path(directory))
            with self.assertRaisesRegex(ConfigurationError, "provider.name"):
                parse_app_config(path)
            self.assertIsInstance(
                parse_app_config(path, provider_catalog=extended).provider, EchoSettings
            )

    def test_credentials_keep_override_environment_and_explicit_dotenv_precedence(
        self,
    ) -> None:
        catalog = ProviderCatalog((echo_registration(),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _yaml(root)
            dotenv = root / "selected.env"
            dotenv.write_text('ECHO_API_KEY="dotenv-value"\n', encoding="utf-8")
            for override, environ, expected, source in (
                (
                    "override-value",
                    {"ECHO_API_KEY": "env-value"},
                    "override-value",
                    "explicit",
                ),
                (None, {"ECHO_API_KEY": "env-value"}, "env-value", "process_env"),
                (None, {}, "dotenv-value", "dotenv"),
            ):
                with self.subTest(source=source):
                    loaded = load_app_config(
                        path,
                        provider_catalog=catalog,
                        api_key_override=override,
                        environ=environ,
                        env_path=dotenv,
                    )
                    self.assertEqual(loaded.provider_config.api_key, expected)
                    self.assertEqual(loaded.credential.source, source)
            with self.assertRaises(ConfigurationError):
                load_app_config(path, provider_catalog=catalog, environ={})

    def test_inline_secret_and_malformed_yaml_are_rejected_before_callback(
        self,
    ) -> None:
        calls: list[object] = []

        def parser(raw):
            calls.append(raw)
            return parse_echo_settings(raw)

        catalog = ProviderCatalog(
            (replace(echo_registration(), parse_settings=parser),)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for body, message in (
                ("  api_key: test-inline-value\n", "inline provider.api_key"),
                ("  model: duplicate\n", "invalid YAML"),
                ("  42: invalid\n", "keys must be strings"),
            ):
                with self.subTest(body=body):
                    with self.assertRaisesRegex(ConfigurationError, message) as caught:
                        parse_app_config(_yaml(root, body), provider_catalog=catalog)
                    self.assertNotIn("test-inline-value", str(caught.exception))
        self.assertEqual(calls, [])

    def test_native_callback_errors_have_stable_safe_messages_and_causes(self) -> None:
        def broken(raw):
            raise ValueError("private parser diagnostic")

        catalog = ProviderCatalog(
            (replace(echo_registration(), parse_settings=broken),)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = _yaml(Path(directory))
            with self.assertRaises(ConfigurationError) as caught:
                load_app_config(path, provider_catalog=catalog, environ={})
            self.assertIsInstance(caught.exception.__cause__, ValueError)
            self.assertNotIn("private", str(caught.exception))
            for invalid in (
                None,
                EchoSettings(api_key_env="INVALID-NAME"),
                EchoSettings(model=" "),
            ):
                with self.subTest(invalid=invalid):
                    catalog = ProviderCatalog(
                        (
                            replace(
                                echo_registration(),
                                parse_settings=lambda raw: cast(
                                    ProviderSettings, invalid
                                ),
                            ),
                        )
                    )
                    with self.assertRaises(ConfigurationError):
                        parse_app_config(path, provider_catalog=catalog)

    def test_factory_only_registration_retains_manual_build_but_requires_yaml_parser(
        self,
    ) -> None:
        catalog = ProviderCatalog((ProviderRegistration("echo", make_echo_provider),))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigurationError, "no settings parser"):
                parse_app_config(_yaml(Path(directory)), provider_catalog=catalog)

    def test_async_settings_parser_is_rejected_before_it_can_allocate_work(
        self,
    ) -> None:
        async def parser(raw):
            return EchoSettings()

        with self.assertRaisesRegex(TypeError, "synchronous"):
            replace(echo_registration(), parse_settings=parser)

    def test_runtime_config_builder_errors_are_classified_without_exposing_values(
        self,
    ) -> None:
        class BrokenSettings(EchoSettings):
            def build(self, api_key: str) -> EchoConfig:
                raise ValueError("private config diagnostic")

        class InvalidSettings(EchoSettings):
            def build(self, api_key: str) -> EchoConfig:
                return cast(EchoConfig, None)

        with tempfile.TemporaryDirectory() as directory:
            path = _yaml(Path(directory))
            for settings in (BrokenSettings(), InvalidSettings()):
                catalog = ProviderCatalog(
                    (replace(echo_registration(), parse_settings=lambda raw: settings),)
                )
                with self.subTest(settings=type(settings).__name__):
                    with self.assertRaises(ConfigurationError) as caught:
                        load_app_config(
                            path,
                            provider_catalog=catalog,
                            api_key_override="test-value",
                        )
                    self.assertNotIn("private", str(caught.exception))
                    self.assertNotIn("test-value", str(caught.exception))


class CatalogRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_configuration_reaches_runtime_trace_and_owned_cleanup(
        self,
    ) -> None:
        providers: list[EchoProvider] = []
        events: list[AgentEvent] = []

        def factory(config: ProviderRuntimeConfig) -> EchoProvider:
            provider = make_echo_provider(config)
            providers.append(provider)
            return provider

        catalog = default_provider_catalog().with_registration(
            replace(echo_registration(), factory=factory)
        )
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                _yaml(Path(directory), "  prefix: 'custom '\n"),
                provider_catalog=catalog,
                api_key_override="offline-demo-key",
                environ={},
            )
            runtime = await (
                AgentRuntimeBuilder.from_loaded_config(loaded, provider_catalog=catalog)
                .with_event_sink(CallbackEventSink(events.append))
                .build()
            )
        async with runtime:
            result = await runtime.run("external", "hello")
            self.assertEqual(result.message.content, "custom hello")
            self.assertEqual(result.usage.total_tokens, 3)
        self.assertTrue(providers[0].closed)
        attempts = [
            event for event in events if event.kind == "provider_attempt_started"
        ]
        self.assertEqual(
            [event.payload["provider_name"] for event in attempts], ["echo"]
        )
        self.assertNotIn("offline-demo-key", repr(events))

    async def test_cancellation_during_async_factory_closes_earlier_resources(
        self,
    ) -> None:
        entered = asyncio.Event()
        released: list[str] = []

        async def factory(config: ProviderRuntimeConfig) -> ModelProvider:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.append("factory")
            raise AssertionError("unreachable")

        class Sink:
            def emit(self, event: AgentEvent) -> None:
                pass

            async def aclose(self) -> None:
                released.append("observer")

        catalog = ProviderCatalog((replace(echo_registration(), factory=factory),))
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                _yaml(Path(directory)),
                provider_catalog=catalog,
                api_key_override="test-value",
            )
        builder = AgentRuntimeBuilder.from_loaded_config(
            loaded, provider_catalog=catalog
        ).with_event_sink(Sink(), ownership="owned")
        task = asyncio.create_task(builder.build())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(released, ["factory", "observer"])

    async def test_factory_only_manual_build_and_runtime_cancel(self) -> None:
        catalog = ProviderCatalog((ProviderRegistration("echo", make_echo_provider),))
        built = await catalog.build("echo", EchoConfig("test-value"))
        self.assertTrue(built.supports_tools)
        await built.provider.aclose()
        selected: list[EchoProvider] = []

        def factory(config: ProviderRuntimeConfig) -> EchoProvider:
            selected.append(make_echo_provider(config))
            return selected[-1]

        catalog = ProviderCatalog((replace(echo_registration(), factory=factory),))
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                _yaml(Path(directory)),
                provider_catalog=catalog,
                api_key_override="test-value",
            )
        runtime = await AgentRuntimeBuilder.from_loaded_config(
            loaded, provider_catalog=catalog
        ).build()
        async with runtime, await runtime.open_session("s") as session:
            handle = await session.submit("wait")
            await selected[0].blocked.wait()
            await handle.abort()
            with self.assertRaises(asyncio.CancelledError):
                await handle.result()
            self.assertEqual(
                [event.kind async for event in handle.events()][-2:],
                ["run_cancelled", "run_settled"],
            )
        self.assertTrue(selected[0].closed)

    async def test_later_assembly_failure_rolls_back_custom_provider(self) -> None:
        closed: list[str] = []

        class ClosableProvider(FakeProvider):
            async def aclose(self) -> None:
                closed.append("provider")

        provider = ClosableProvider([[Finish()]])
        catalog = ProviderCatalog(
            (replace(echo_registration(), factory=lambda config: provider),)
        )
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_app_config(
                _yaml(Path(directory)),
                provider_catalog=catalog,
                api_key_override="test-value",
            )
        with self.assertRaises(RuntimeBuildError):
            await (
                AgentRuntimeBuilder.from_loaded_config(loaded, provider_catalog=catalog)
                .with_graceful_shutdown(-1)
                .build()
            )
        self.assertEqual(closed, ["provider"])
