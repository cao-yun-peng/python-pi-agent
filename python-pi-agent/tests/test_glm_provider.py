from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from pi_agent_py import (
    ConfigurationError,
    Finish,
    Message,
    ProviderAuthenticationError,
    ProviderBalanceError,
    ProviderError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderRequestContext,
    ProviderResponseError,
    ProviderTimeoutError,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from pi_agent_py.providers import GLMConfig, GLMProvider


class GLMProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_reasoning_text_usage_and_standard_request_fields(self) -> None:
        captured: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers["Authorization"]
            captured["body"] = json.loads(request.content)
            return _sse_response(
                'data: {"choices":[{"delta":{"reasoning_content":"先想"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6,"prompt_tokens_details":{"cached_tokens":1}}}',
                "data: [DONE]",
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = GLMProvider(
            GLMConfig(
                api_key="test-key",
                thinking="enabled",
                reasoning_effort="high",
                max_tokens=2048,
                user_id="learner-01",
            ),
            client=client,
        )

        events = await _collect(provider, [Message(role="user", content="你好")])

        self.assertEqual(
            events,
            [
                ThinkingDelta("先想"),
                TextDelta("你好"),
                TokenUsage(4, 2, 6, 1),
                Finish("stop"),
            ],
        )
        self.assertEqual(
            captured["url"],
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        )
        self.assertEqual(captured["authorization"], "Bearer test-key")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "glm-5.3")
        self.assertEqual(body["request_id"], "request-test")
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["max_tokens"], 2048)
        self.assertEqual(body["user_id"], "learner-01")
        self.assertNotIn("stream_options", body)

    async def test_aggregates_tool_fragments_and_enables_tool_stream(self) -> None:
        captured_body: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content))
            return _sse_response(
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"weather","arguments":"{\\"city\\":"}}]},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"杭州\\"}"}}]},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
                "data: [DONE]",
            )

        definition = ToolDefinition(
            name="weather",
            description="Read weather.",
            input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
            side_effects="read",
            retry_safe=True,
            replay_safe=True,
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)

        events = await _collect(
            GLMProvider(GLMConfig(api_key="test-key"), client=client),
            [Message(role="user", content="杭州天气")],
            [definition],
        )

        self.assertEqual(
            events,
            [ToolCall("call-1", "weather", {"city": "杭州"}), Finish("tool_calls")],
        )
        self.assertIs(captured_body["tool_stream"], True)
        tools = captured_body["tools"]
        assert isinstance(tools, list)
        self.assertEqual(tools[0]["function"]["name"], "weather")

    async def test_replays_only_current_tool_loop_reasoning(self) -> None:
        captured_body: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content))
            return _sse_response(
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
            )

        history = [
            Message(role="user", content="旧问题"),
            Message(role="assistant", content="旧回答", thinking="旧思考"),
            Message(role="user", content="新问题"),
            Message(
                role="assistant",
                content="",
                thinking="当前工具思考",
                tool_calls=(ToolCall("call-1", "weather", {"city": "杭州"}),),
            ),
            Message(
                role="tool",
                content="晴",
                tool_call_id="call-1",
                tool_name="weather",
            ),
        ]
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)

        await _collect(GLMProvider(GLMConfig(api_key="test-key"), client=client), history)

        messages = captured_body["messages"]
        assert isinstance(messages, list)
        self.assertNotIn("reasoning_content", messages[1])
        self.assertEqual(messages[3]["reasoning_content"], "当前工具思考")

    def test_validates_model_capabilities_and_standard_limits(self) -> None:
        invalid = (
            ({"thinking": "disabled"}, "cannot be disabled"),
            ({"reasoning_effort": "medium"}, "supports only"),
            (
                {"model": "glm-5", "reasoning_effort": "high"},
                "does not support reasoning_effort",
            ),
            (
                {"model": "custom-glm", "thinking": "enabled"},
                "unknown GLM models",
            ),
            ({"max_tokens": 131073}, "at most 131072"),
            ({"user_id": "short"}, "6 to 128"),
        )
        for kwargs, message in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ConfigurationError, message):
                    GLMConfig(api_key="secret", **kwargs)  # type: ignore[arg-type]

        self.assertEqual(
            GLMConfig(
                api_key="secret",
                model="glm-5.2",
                reasoning_effort="medium",
            ).reasoning_effort,
            "medium",
        )
        self.assertEqual(
            GLMConfig(api_key="secret", model="custom-glm").model,
            "custom-glm",
        )

    async def test_maps_official_error_codes_without_leaking_key(self) -> None:
        cases = (
            (
                401,
                {"error": {"code": "1001", "message": "invalid token"}},
                ProviderAuthenticationError,
            ),
            (
                429,
                {"error": {"code": "1113", "message": "insufficient balance"}},
                ProviderBalanceError,
            ),
            (
                429,
                {"error": {"code": "1302", "message": "rate limited"}},
                ProviderRateLimitError,
            ),
            (
                503,
                {"error": {"code": "1305", "message": "model busy"}},
                ProviderHTTPError,
            ),
        )
        for status, body, error_type in cases:
            with self.subTest(status=status, body=body):
                provider, client = _provider_for(httpx.Response(status, json=body))
                try:
                    with self.assertRaises(error_type) as raised:
                        await _collect(provider, [Message(role="user", content="hello")])
                    self.assertNotIn("super-secret", str(raised.exception))
                    if isinstance(raised.exception, ProviderHTTPError):
                        self.assertTrue(raised.exception.retryable)
                finally:
                    await client.aclose()

    async def test_rejects_incomplete_or_non_sse_stream(self) -> None:
        cases = (
            (
                httpx.Response(200, headers={"Content-Type": "application/json"}),
                "not text/event-stream",
            ),
            (
                _sse_response(
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                ),
                r"before the \[DONE\]",
            ),
            (
                _sse_response("data: [DONE]"),
                "without a finish reason",
            ),
        )
        for response, message in cases:
            with self.subTest(message=message):
                provider, client = _provider_for(response)
                try:
                    with self.assertRaisesRegex(ProviderResponseError, message):
                        await _collect(provider, [Message(role="user", content="hello")])
                finally:
                    await client.aclose()

    async def test_timeout_cancellation_and_close_lifecycle(self) -> None:
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        timeout_client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        self.addAsyncCleanup(timeout_client.aclose)
        timeout_provider = GLMProvider(
            GLMConfig(api_key="secret", timeout_seconds=3),
            client=timeout_client,
        )
        with self.assertRaisesRegex(ProviderTimeoutError, "timed out after 3s"):
            await _collect(timeout_provider, [Message(role="user", content="hello")])

        reached = asyncio.Event()

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[override]
                yield b'data: {"choices":[{"delta":{"content":"a"},"finish_reason":null}]}\n\n'
                reached.set()
                await asyncio.Event().wait()

        def blocking(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=BlockingStream(),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(blocking))
        self.addAsyncCleanup(client.aclose)
        provider = GLMProvider(GLMConfig(api_key="secret"), client=client)
        task = asyncio.create_task(
            _collect(provider, [Message(role="user", content="hello")])
        )
        await asyncio.wait_for(reached.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        await provider.aclose()
        self.assertFalse(client.is_closed)
        with self.assertRaisesRegex(ProviderError, "closed"):
            await _collect(provider, [Message(role="user", content="hello")])


def _sse_response(*events: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "Text/Event-Stream; charset=utf-8"},
        content=("\n\n".join(events) + "\n\n").encode(),
    )


def _provider_for(response: httpx.Response) -> tuple[GLMProvider, httpx.AsyncClient]:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return GLMProvider(GLMConfig(api_key="super-secret"), client=client), client


async def _collect(
    provider: GLMProvider,
    messages: list[Message],
    tools: list[ToolDefinition] | None = None,
) -> list[object]:
    context = ProviderRequestContext(
        request_id="request-test",
        attempt_id="attempt-test",
        purpose="agent_turn",
        request_number=1,
        run_id="run-test",
        turn_id="run-test:turn:1",
    )
    return [
        event
        async for event in provider.stream(messages, tools or [], context=context)
    ]


if __name__ == "__main__":
    unittest.main()
