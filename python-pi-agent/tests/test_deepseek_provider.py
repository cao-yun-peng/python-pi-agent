from __future__ import annotations

import json
import unittest

import httpx

from pi_agent_py import (
    Agent,
    DeclaredSideEffectSandbox,
    ConfigurationError,
    Finish,
    FunctionTool,
    InMemoryEventSink,
    JSONObject,
    Message,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
)
from pi_agent_py.providers import DeepSeekConfig, DeepSeekProvider


class DeepSeekProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_stream_and_integrates_with_agent(self) -> None:
        captured_request: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured_request["authorization"] = request.headers["Authorization"]
            captured_request["body"] = json.loads(request.content)
            body = "\n".join(
                [
                    ": keep-alive",
                    "",
                    'data: {"choices":[{"delta":{"reasoning_content":"先思考"},"finish_reason":null}]}',
                    "",
                    'data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}',
                    "",
                    'data: {"choices":[{"delta":{"content":"好"},"finish_reason":null}]}',
                    "",
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":3,"total_tokens":7,"prompt_tokens_details":{"cached_tokens":2}}}',
                    "",
                    "data: [DONE]",
                    "",
                ]
            )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=body.encode(),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = DeepSeekProvider(
            DeepSeekConfig(
                api_key="test-key",
                thinking="enabled",
                reasoning_effort="high",
            ),
            client=client,
        )
        events = InMemoryEventSink()
        agent = Agent(provider, event_sink=events)

        result = await agent.run("打个招呼")

        self.assertEqual(
            result.message,
            Message(role="assistant", content="你好", thinking="先思考"),
        )
        self.assertIsNotNone(result.usage)
        assert result.usage is not None
        self.assertEqual(result.usage.total_tokens, 7)
        self.assertEqual(result.usage.cached_input_tokens, 2)
        self.assertIn("thinking_delta", [event.kind for event in events.events])
        self.assertIn("model_usage", [event.kind for event in events.events])
        self.assertEqual(captured_request["authorization"], "Bearer test-key")
        request_body = captured_request["body"]
        assert isinstance(request_body, dict)
        self.assertEqual(request_body["model"], "deepseek-flash")
        self.assertEqual(request_body["thinking"], {"type": "enabled"})
        self.assertEqual(request_body["reasoning_effort"], "high")
        self.assertTrue(request_body["stream"])

    async def test_maps_authentication_error_without_exposing_api_key(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                401,
                json={"error": {"message": "Authentication Fails"}},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        config = DeepSeekConfig(api_key="super-secret")
        provider = DeepSeekProvider(config, client=client)

        with self.assertRaisesRegex(
            ProviderAuthenticationError,
            "Authentication Fails",
        ) as raised:
            await _collect(provider, [Message(role="user", content="hello")])

        self.assertNotIn("super-secret", str(raised.exception))
        self.assertNotIn("super-secret", repr(config))

    async def test_normalizes_split_tool_call_and_sends_tool_schema(self) -> None:
        captured_request: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured_request["body"] = json.loads(request.content)
            body = "\n".join(
                [
                    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"weather","arguments":"{\\"city\\":"}}]},"finish_reason":null}]}',
                    "",
                    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"深圳\\"}"}}]},"finish_reason":null}]}',
                    "",
                    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
                    "",
                    "data: [DONE]",
                    "",
                ]
            )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=body.encode(),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = DeepSeekProvider(DeepSeekConfig(api_key="test-key"), client=client)
        definition = ToolDefinition(
            name="weather",
            description="Read weather for one city.",
            input_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            side_effects="read",
            retry_safe=True,
            replay_safe=True,
        )

        events = await _collect(
            provider,
            [Message(role="user", content="深圳天气")],
            [definition],
        )

        self.assertEqual(
            events,
            [
                ToolCall("call-1", "weather", {"city": "深圳"}),
                Finish("tool_calls"),
            ],
        )
        request_body = captured_request["body"]
        assert isinstance(request_body, dict)
        tools = request_body["tools"]
        assert isinstance(tools, list)
        self.assertEqual(tools[0]["function"]["name"], "weather")

    async def test_replays_reasoning_content_after_thinking_tool_call(self) -> None:
        captured_bodies: list[dict[str, object]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
            if len(captured_bodies) == 1:
                body = "\n".join(
                    [
                        'data: {"choices":[{"delta":{"reasoning_content":"先查实时天气"},"finish_reason":null}]}',
                        "",
                        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-weather","type":"function","function":{"name":"weather","arguments":"{\\"city\\":\\"深圳\\"}"}}]},"finish_reason":null}]}',
                        "",
                        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
                        "",
                        "data: [DONE]",
                        "",
                    ]
                )
            else:
                body = "\n".join(
                    [
                        'data: {"choices":[{"delta":{"reasoning_content":"根据工具结果回答"},"finish_reason":null}]}',
                        "",
                        'data: {"choices":[{"delta":{"content":"深圳晴"},"finish_reason":null}]}',
                        "",
                        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                        "",
                        "data: [DONE]",
                        "",
                    ]
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=body.encode(),
            )

        async def weather(
            arguments: JSONObject,
            context: ToolExecutionContext,
        ) -> ToolOutput:
            del context
            self.assertEqual(arguments, {"city": "深圳"})
            return ToolOutput(content="晴")

        definition = ToolDefinition(
            name="weather",
            description="Read weather for one city.",
            input_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            side_effects="read",
            retry_safe=True,
            replay_safe=True,
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = DeepSeekProvider(
            DeepSeekConfig(api_key="test-key", thinking="enabled"),
            client=client,
        )
        agent = Agent(
            provider,
            tools=[FunctionTool(definition, weather)],
            sandbox=DeclaredSideEffectSandbox(
                allowed_side_effects=("none", "read")
            ),
        )

        result = await agent.run("深圳天气如何？")

        self.assertEqual(result.message.content, "深圳晴")
        self.assertEqual(result.messages[1].thinking, "先查实时天气")
        self.assertEqual(result.message.thinking, "根据工具结果回答")
        self.assertEqual(len(captured_bodies), 2)
        second_messages = captured_bodies[1]["messages"]
        assert isinstance(second_messages, list)
        assistant_message = second_messages[1]
        assert isinstance(assistant_message, dict)
        self.assertEqual(
            assistant_message["reasoning_content"],
            "先查实时天气",
        )

    async def test_rejects_malformed_stream_json(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=b"data: not-json\n\n",
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = DeepSeekProvider(
            DeepSeekConfig(api_key="test-key"),
            client=client,
        )

        with self.assertRaisesRegex(ProviderResponseError, "malformed SSE JSON"):
            await _collect(provider, [Message(role="user", content="hello")])

    async def test_rejects_non_sse_success_response(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, json={"unexpected": "response"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = DeepSeekProvider(
            DeepSeekConfig(api_key="test-key"),
            client=client,
        )

        with self.assertRaisesRegex(ProviderResponseError, "text/event-stream"):
            await _collect(provider, [Message(role="user", content="hello")])

    async def test_maps_rate_limit_error(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                429,
                json={"error": {"message": "Rate Limit Reached"}},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = DeepSeekProvider(
            DeepSeekConfig(api_key="test-key"),
            client=client,
        )

        with self.assertRaisesRegex(ProviderRateLimitError, "Rate Limit Reached"):
            await _collect(provider, [Message(role="user", content="hello")])

    async def test_owned_client_close_is_idempotent(self) -> None:
        provider = DeepSeekProvider(DeepSeekConfig(api_key="test-key"))

        await provider.aclose()
        await provider.aclose()

        with self.assertRaisesRegex(ProviderError, "closed"):
            await _collect(provider, [Message(role="user", content="hello")])

    async def test_external_client_is_not_closed_by_provider(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
        self.addAsyncCleanup(client.aclose)
        provider = DeepSeekProvider(
            DeepSeekConfig(api_key="test-key"),
            client=client,
        )

        await provider.aclose()

        self.assertFalse(client.is_closed)

    def test_validates_configuration_without_revealing_secret(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must not be empty"):
            DeepSeekConfig(api_key="  ")
        with self.assertRaisesRegex(ConfigurationError, "requires thinking"):
            DeepSeekConfig(
                api_key="secret",
                thinking="disabled",
                reasoning_effort="high",
            )


async def _collect(
    provider: DeepSeekProvider,
    messages: list[Message],
    tools: list[ToolDefinition] | None = None,
) -> list[object]:
    return [event async for event in provider.stream(messages, tools or [])]


if __name__ == "__main__":
    unittest.main()
