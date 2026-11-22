"""Offline contracts for static prompt assembly and durable reuse."""

import asyncio
import unittest
from dataclasses import replace

from pi_agent_py.api import (
    Agent, AgentComponents, AgentProfile, AgentRuntimeBuilder, AgentSession,
    ConfigurationError, Finish, FunctionTool, InMemorySessionStore, SessionSpec,
    SystemPromptSpec, TextDelta, ToolDefinition, ToolOutput, coding_profile,
)
from pi_agent_py.runtime.prompts import render_system_prompt
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled, InMemoryWorkspaceFiles
from pi_agent_py.api import ReadTool, WriteTool, EditTool, ProviderError, Message
from pi_agent_py.tools.shell import BashTool
from pi_agent_py.api import CompactionComponents, ModelCompactionStrategy


async def _execute(arguments, context):
    return ToolOutput("ok")


def _tool(name, *rules):
    return FunctionTool(ToolDefinition(
        name, "Test tool", {"type": "object"}, prompt_guidelines=rules,
    ), _execute)


class PromptValueTests(unittest.TestCase):
    def test_builtin_rules_follow_local_contracts(self):
        files = InMemoryWorkspaceFiles({})
        class Executor:
            async def run(self, spec):
                raise AssertionError("rendering must not execute tools")

        definitions = tuple(tool.definition for tool in (
            ReadTool(files), WriteTool(files), EditTool(files), BashTool(Executor()),
        ))
        text = render_system_prompt(SystemPromptSpec("Role"), definitions)
        for requirement in ("next_offset", "expected_version", "exactly once", "workdir"):
            self.assertIn(requirement, text)
        self.assertEqual(text.count("Never automatically retry a file mutation"), 1)

    def test_rendering_is_ordered_deduplicated_and_optional(self):
        spec = SystemPromptSpec("Role", (" Shared ", "", "First"))
        definitions = (_tool("one", "Shared", "Second").definition,)
        self.assertEqual(render_system_prompt(spec, definitions),
                         "Role\n\n<rules>\n- Shared\n- First\n- Second\n</rules>")
        self.assertNotIn("Second", render_system_prompt(
            replace(spec, include_tool_guidelines=False), definitions))
        self.assertEqual(render_system_prompt(SystemPromptSpec("Role"), ()), "Role")

    def test_values_are_detached_and_invalid_inputs_rejected(self):
        rules = ["One"]
        spec = SystemPromptSpec("Role", rules)
        definition = ToolDefinition("x", "X", {"type": "object"}, prompt_guidelines=rules)
        rules.append("Two")
        self.assertEqual(spec.guidelines, ("One",))
        self.assertEqual(definition.prompt_guidelines, ("One",))
        for create in (
            lambda: SystemPromptSpec(" "),
            lambda: SystemPromptSpec("Role", "characters"),
            lambda: SystemPromptSpec("Role", (42,)),
            lambda: SystemPromptSpec("Role", include_tool_guidelines=1),
            lambda: ToolDefinition("x", "X", {"type": "object"}, prompt_guidelines="bad"),
        ):
            with self.subTest(create=create), self.assertRaises((TypeError, ValueError)):
                create()
        with self.assertRaises(ConfigurationError):
            AgentComponents(FakeProvider([]), system_prompt="Exact", prompt_spec=spec)
        with self.assertRaises(ConfigurationError):
            AgentProfile("bad", system_prompt="Exact", prompt_spec=spec)

    def test_session_spec_compares_prompt_specs_by_value(self):
        base = AgentComponents(FakeProvider([]), prompt_spec=SystemPromptSpec("Role"))
        self.assertTrue(SessionSpec(base)._matches(SessionSpec(base.replace(
            prompt_spec=SystemPromptSpec("Role")))))
        self.assertFalse(SessionSpec(base)._matches(SessionSpec(base.replace(
            prompt_spec=SystemPromptSpec("Changed")))))


class PromptIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_keeps_rendered_system_prompt(self):
        provider = FakeProvider([[TextDelta("answer"), Finish()] for _ in range(3)])
        summary = FakeProvider([[TextDelta("Checkpoint"), Finish()]])
        components = coding_profile().build_components(provider).replace(
            compaction=CompactionComponents(ModelCompactionStrategy(summary)))
        async with AgentSession("s", InMemorySessionStore(), components) as session:
            await session.run("one")
            await session.run("two")
            snapshot = await session.compact(keep_recent_turns=1)
            await session.run("three")
        self.assertEqual(snapshot.messages[0], provider.requests[0][0])
        self.assertEqual(provider.requests[-1][0], provider.requests[0][0])
        self.assertEqual(summary.requested_tools, [()])

    async def test_failure_preserves_prompt_and_legacy_history_is_not_migrated(self):
        store = InMemorySessionStore()
        provider = FakeProvider([[ProviderError("offline failure")], [Finish()]])
        base = AgentComponents(provider, prompt_spec=SystemPromptSpec("Role"))
        async with AgentSession("s", store, base) as session:
            with self.assertRaises(ProviderError):
                await session.run("one")
            await session.run("two")
        self.assertEqual(sum(m.role == "system" for m in (await store.load("s")).messages), 1)
        legacy = FakeProvider([[Finish()]])
        await store.append("legacy", expected_version=0, messages=(Message("user", "old"),))
        async with AgentSession("legacy", store, base.replace(provider=legacy)) as session:
            await session.run("continue")
        self.assertFalse(any(m.role == "system" for m in legacy.requests[0]))

    async def test_minimal_custom_and_disabled_prompts(self):
        for coding, override, expected in (
            (False, "unset", None), (True, "unset", "coding"),
            (True, "  Exact text.  ", "  Exact text.  "), (True, None, None),
        ):
            provider = FakeProvider([[TextDelta("ok"), Finish()]])
            builder = AgentRuntimeBuilder().with_provider(provider)
            if coding:
                builder.with_profile(coding_profile())
            if override != "unset":
                builder.with_system_prompt(override)
            async with await builder.build() as runtime:
                await runtime.run("s", "Question")
            systems = [m.content for m in provider.requests[0] if m.role == "system"]
            if expected == "coding":
                self.assertIn("coding", systems[0])
                self.assertNotIn("bash", systems[0])
            else:
                self.assertEqual(systems, [] if expected is None else [expected])

    async def test_plugin_tools_are_included_and_session_selection_isolated(self):
        selected = _tool("selected", "Use selected.")
        plugin_tool = _tool("plugin_tool", "Use plugin_tool.")
        provider = FakeProvider([[TextDelta("one"), Finish()], [TextDelta("two"), Finish()]])

        class Resolver:
            def __init__(self, base):
                self.base = base

            async def resolve(self, session_id, request):
                return SessionSpec(self.base if session_id == "all" else self.base.replace(
                    tools=(selected,), prompt_spec=SystemPromptSpec("Selected role")))

        def plugin(api):
            api.register_tool(plugin_tool)

        runtime = await (AgentRuntimeBuilder().with_provider(provider)
                         .with_profile(coding_profile()).with_tools((selected,))
                         .with_plugin("extra", plugin)
                         .with_session_spec_resolver_factory(Resolver).build())
        async with runtime:
            await runtime.run("all", "Question")
            await runtime.run("selected", "Question")
        self.assertIn("Use plugin_tool.", provider.requests[0][0].content)
        self.assertIn("Use selected.", provider.requests[1][0].content)
        self.assertNotIn("plugin_tool", provider.requests[1][0].content)

    async def test_durable_prompt_is_written_once_and_reused_on_resume(self):
        store = InMemorySessionStore()
        provider = FakeProvider([[TextDelta("ok"), Finish()] for _ in range(3)])
        base = AgentComponents(provider, prompt_spec=SystemPromptSpec("Original"))
        async with AgentSession("s", store, base) as session:
            await session.run("one")
            await session.run("two")
        async with AgentSession("s", store, base.replace(
                prompt_spec=SystemPromptSpec("New default"))) as session:
            await session.run("three")
        snapshot = await store.load("s")
        self.assertEqual([m.content for m in snapshot.messages if m.role == "system"], ["Original"])
        self.assertEqual(provider.requests[-1][0].content, "Original")

    async def test_standalone_uses_same_renderer_and_cancellation_keeps_prompt(self):
        provider = FakeProvider([[WaitUntilCancelled()]])
        components = coding_profile().build_components(provider).replace(tools=(_tool("x", "Use x."),))
        agent = Agent(components)
        self.assertIn("Use x.", agent.messages[0].content)
        with self.assertRaisesRegex(ValueError, "initial_messages"):
            Agent(components, initial_messages=agent.messages)
        task = asyncio.create_task(agent.run("Question"))
        await provider.blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sum(m.role == "system" for m in agent.messages), 1)
