"""Approve and sandbox one tool call with an inspectable execution context."""

from __future__ import annotations

import asyncio

from pi_agent_py import (
    Agent,
    DeclaredSideEffectSandbox,
    FakeProvider,
    Finish,
    FunctionTool,
    JSONObject,
    StaticApprovalPolicy,
    TextDelta,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)


async def save_note(
    arguments: JSONObject,
    context: ToolExecutionContext,
) -> ToolOutput:
    print(
        "executing",
        context.call.id,
        "in session",
        context.session_id,
        "approved=",
        context.approval.approved if context.approval else False,
    )
    return ToolOutput(
        content=f"saved: {arguments['text']}",
        structured_content={"saved": True},
    )


async def main() -> None:
    definition = ToolDefinition(
        name="save_note",
        description="Save one note in the configured application store.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        side_effects="write",
        requires_approval=True,
        retry_safe=False,
        replay_safe=False,
    )
    provider = FakeProvider(
        [
            [
                ToolCall("save-1", "save_note", {"text": "learn sandbox seams"}),
                Finish("tool_calls"),
            ],
            [TextDelta("The note was saved."), Finish()],
        ]
    )
    sandbox = DeclaredSideEffectSandbox(
        allowed_side_effects=("none", "write"),
        allowed_tools=("save_note",),
    )
    agent = Agent(
        provider,
        tools=(FunctionTool(definition, save_note),),
        approval_policy=StaticApprovalPolicy(
            True,
            reason="The example pre-authorizes this one tool.",
        ),
        sandbox=sandbox,
        session_id="security-demo",
    )

    result = await agent.run("Save a learning note.")
    print(result.message.content)


if __name__ == "__main__":
    asyncio.run(main())
