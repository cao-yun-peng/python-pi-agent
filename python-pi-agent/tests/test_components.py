import unittest
from dataclasses import FrozenInstanceError

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

    async def test_agent_preserves_legacy_keywords_and_warns(self) -> None:
        for legacy in (False, True):
            provider = FakeProvider([[TextDelta("answer"), Finish()]])
            if legacy:
                with self.assertWarns(DeprecationWarning):
                    agent = Agent(provider=provider, system_prompt="Be concise.")
            else:
                agent = Agent(
                    components=AgentComponents(provider, system_prompt="Be concise.")
                )
            self.assertEqual((await agent.run("question")).message.content, "answer")
            self.assertEqual(provider.requests[0][0].content, "Be concise.")

    async def test_session_and_runtime_legacy_constructors_warn_and_run(self) -> None:
        for entry in (AgentSession, AgentRuntime):
            with self.subTest(entry=entry.__name__):
                provider = FakeProvider([[TextDelta("answer"), Finish()]])
                store = InMemorySessionStore()
                if entry is AgentSession:
                    with self.assertWarns(DeprecationWarning):
                        session = AgentSession("demo", store, provider=provider)
                    async with session:
                        result = await session.run("question")
                else:
                    with self.assertWarns(DeprecationWarning):
                        runtime = AgentRuntime(provider=provider, session_store=store)
                    async with runtime:
                        result = await runtime.run("demo", "question")
                self.assertEqual(result.message.content, "answer")

    def test_components_reject_legacy_overrides(self) -> None:
        with self.assertRaisesRegex(TypeError, "cannot be combined"):
            Agent(AgentComponents(FakeProvider([])), max_model_requests=2)
