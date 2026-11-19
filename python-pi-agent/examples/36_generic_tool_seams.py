"""Exercise the generic MCP prerequisites without a server, socket or paid model."""

import asyncio

from pi_agent_py.api import (
    AgentEvent,
    AgentRuntimeBuilder,
    CallbackEventSink,
    ConfigurationError,
    Finish,
    FunctionTool,
    JSONObject,
    JsonSchemaValidator,
    PluginAPI,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from pi_agent_py.testing import FakeProvider


async def main() -> None:
    validator = JsonSchemaValidator()
    schema = {
        "type": "object",
        "properties": {
            "code": {
                "anyOf": [
                    {"type": "string", "pattern": "^OK-[0-9]+$"},
                    {"type": "null"},
                ],
            }
        },
        "required": ["code"],
        "additionalProperties": False,
    }
    approval = StaticApprovalPolicy(True)

    async def worker(context: ToolExecutionContext) -> ToolOutput:
        await asyncio.sleep(0)
        print("Worker received identity:", context.run_id, context.turn_id)
        return ToolOutput("Code accepted")

    async def accept(
        arguments: JSONObject, context: ToolExecutionContext
    ) -> ToolOutput:
        return await asyncio.create_task(worker(context))

    async def wait(arguments: JSONObject, context: ToolExecutionContext) -> ToolOutput:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    def plugin(api: PluginAPI) -> None:
        if not validator.supports(schema):
            raise ConfigurationError("unsupported tool schema")
        print("Plugin borrows observer:", type(api.event_sink).__name__)
        api.register_tool(
            FunctionTool(
                ToolDefinition(
                    "accept", "Accept an example code", schema, requires_approval=True
                ),
                accept,
            )
        )
        api.register_tool(
            FunctionTool(
                ToolDefinition(
                    "wait",
                    "Wait for a deterministic timeout",
                    {"type": "object"},
                    timeout_seconds=0,
                ),
                wait,
            )
        )
        api.register_cleanup(lambda: print("Plugin cleanup completed"))

    def trace(event: AgentEvent) -> None:
        if event.kind in {
            "plugin_loaded",
            "tool_validation_failed",
            "tool_approval_requested",
            "tool_execution_started",
            "tool_execution_finished",
            "run_settled",
        }:
            print(event.kind, event.envelope.run_id, event.envelope.turn_id)

    provider = FakeProvider(
        [
            [
                ToolCall("bad", "accept", {"code": "wrong"}),
                ToolCall("good", "accept", {"code": "OK-12"}),
                Finish("tool_calls"),
            ],
            [TextDelta("One valid call executed"), Finish()],
            [ToolCall("timeout", "wait", {}), Finish("tool_calls")],
            [TextDelta("The timeout does not authorize a retry"), Finish()],
        ]
    )
    runtime = await (
        AgentRuntimeBuilder()
        .with_provider(provider)
        .with_schema_validator(validator)
        .with_approval_policy(approval)
        .with_event_sink(CallbackEventSink(trace))
        .with_plugin("schema-demo", plugin)
        .build()
    )
    async with runtime:
        first = await runtime.run("demo", "Check two example codes")
        print("Approval count after validation:", len(approval.requests))
        second = await runtime.run("demo", "Exercise timeout")
    for result in (first, second):
        print(result.message.content)
    print(
        "Timeout result:",
        next(m.content for m in reversed(second.messages) if m.role == "tool"),
    )


if __name__ == "__main__":
    asyncio.run(main())
