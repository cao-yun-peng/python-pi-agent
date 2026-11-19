import unittest
from dataclasses import FrozenInstanceError
from inspect import signature

from pi_agent_py import (
    Agent,
    AgentComponents,
    AgentProfile,
    AgentRuntime,
    AgentSession,
    CompactionComponents,
    ConfigurationError,
    FakeCompactionStrategy,
    FakeProvider,
    Finish,
    FunctionTool,
    InMemorySessionStore,
    JSONObject,
    TextDelta,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from pi_agent_py.api import RuntimeSession


async def _echo(arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
    return ToolOutput(content="echo")


class AgentComponentsTests(unittest.IsolatedAsyncioTestCase):
    def test_components_freeze_tools_and_validate_replacements(self) -> None:
        provider = FakeProvider([])
        tool = FunctionTool(ToolDefinition("echo", "Echo", {"type": "object"}), _echo)
        components = AgentComponents(provider, tools=(tool,))
        updated = components.replace(system_prompt="Be concise.")

        self.assertIsNone(components.system_prompt)
        self.assertIs(updated.provider, provider)
        self.assertEqual(updated.tools, (tool,))
        with self.assertRaises(FrozenInstanceError):
            setattr(components, "system_prompt", "changed")
        with self.assertRaisesRegex(ValueError, "duplicate tool names: echo"):
            components.replace(tools=(tool, tool))
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaisesRegex(
                ValueError, "positive integer"
            ):
                components.replace(max_model_requests=limit)

    def test_compaction_requires_a_strategy_and_allows_manual_mode(self) -> None:
        with self.assertRaises(ConfigurationError):
            CompactionComponents(strategy=None)
        strategy = FakeCompactionStrategy([])
        profile = AgentProfile("manual", compaction_strategy_factory=lambda: strategy)
        compaction = profile.build_components(FakeProvider([])).compaction
        self.assertEqual(compaction, CompactionComponents(strategy=strategy))

    async def test_agent_accepts_components_positionally_or_by_keyword(self) -> None:
        for keyword in (False, True):
            provider = FakeProvider([[TextDelta("answer"), Finish()]])
            components = AgentComponents(provider, system_prompt="Be concise.")
            agent = Agent(components=components) if keyword else Agent(components)
            self.assertEqual((await agent.run("question")).message.content, "answer")
            self.assertEqual(provider.requests[0][0].content, "Be concise.")

    async def test_session_and_runtime_run_with_resolved_components(self) -> None:
        for entry in (AgentSession, AgentRuntime):
            with self.subTest(entry=entry.__name__):
                provider = FakeProvider([[TextDelta("answer"), Finish()]])
                components = AgentComponents(provider)
                store = InMemorySessionStore()
                if entry is AgentSession:
                    session = AgentSession("demo", store, components=components)
                    async with session:
                        result = await session.run("question")
                else:
                    runtime = AgentRuntime(components=components, session_store=store)
                    async with runtime:
                        result = await runtime.run("demo", "question")
                self.assertEqual(result.message.content, "answer")

    def test_constructors_require_components_instead_of_a_raw_provider(self) -> None:
        provider = FakeProvider([])
        store = InMemorySessionStore()
        for create in (
            lambda: Agent(provider),
            lambda: AgentSession("demo", store, provider),
            lambda: AgentRuntime(provider, store),
        ):
            with self.assertRaisesRegex(TypeError, "AgentComponents"):
                create()

    def test_constructors_remove_distributed_component_options(self) -> None:
        components = AgentComponents(FakeProvider([]))
        store = InMemorySessionStore()
        factories = (
            (Agent, lambda **options: Agent(**options)),
            (AgentSession, lambda **options: AgentSession("demo", store, **options)),
            (AgentRuntime, lambda **options: AgentRuntime(session_store=store, **options)),
        )
        removed = (
            "provider", "tools", "approval_policy", "execution_gate", "context_strategy",
            "compaction_policy", "compaction_strategy", "next_turn_hook",
            "system_prompt", "max_model_requests",
        )
        for entry, create in factories:
            self.assertIs(signature(entry).parameters["components"].default, signature(entry).empty)
            for name in removed:
                with self.subTest(entry=entry.__name__, option=name):
                    self.assertNotIn(name, signature(entry).parameters)
                    with self.assertRaises(TypeError):
                        create(components=components, **{name: None})

    def test_agent_rejects_public_wiring_keywords(self) -> None:
        components = AgentComponents(FakeProvider([]))
        for name in (
            "message_committer", "steering_committer", "steering_receiver",
            "provider_request_hook",
        ):
            with self.subTest(option=name), self.assertRaises(TypeError):
                Agent(components, **{name: None})

    def test_deprecated_adapters_are_absent(self) -> None:
        import pi_agent_py.runtime.components as components

        self.assertFalse(hasattr(components, "_resolve_components"))
        self.assertFalse(hasattr(RuntimeSession, "follow_up_id"))
