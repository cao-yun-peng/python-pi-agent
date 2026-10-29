from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from pi_agent_py import (
    Agent,
    ConfigurationError,
    Finish,
    InMemoryEventSink,
    Message,
    ProviderAuthenticationError,
    ProviderBalanceError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestContext,
    ProviderResponseError,
    ProviderTimeoutError,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from pi_agent_py.providers import KimiConfig, KimiProvider


class KimiProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_done_and_emits_usage_before_finish(self) -> None:
        captured_request: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured_request["authorization"] = request.headers["Authorization"]
            captured_request["body"] = json.loads(request.content)
            return _sse_response(
                'data: {"choices":[{"delta":{"reasoning_content":"先想"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6,"prompt_tokens_details":{"cached_tokens":1}}}',
                "data: [DONE]",
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = KimiProvider(KimiConfig(api_key="test-key"), client=client)
        events = InMemoryEventSink()

        result = await Agent(provider, event_sink=events).run("打个招呼")

        self.assertEqual(
            result.message,
            Message(role="assistant", content="你好", thinking="先想"),
        )
        self.assertEqual(result.usage, TokenUsage(4, 2, 6, 1))
        self.assertIn("model_usage", [event.kind for event in events.events])
        self.assertEqual(captured_request["authorization"], "Bearer test-key")
        request_body = captured_request["body"]
        assert isinstance(request_body, dict)
        self.assertEqual(request_body["model"], "kimi-k2.6")
        self.assertEqual(request_body["stream_options"], {"include_usage": True})
        self.assertNotIn("thinking", request_body)

    async def test_normalized_finish_is_last_even_when_usage_arrives_later(self) -> None:
        provider, client = _provider_for(
            _sse_response(
                'data: {"choices":[{"delta":{"content":"a"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cached_tokens":1,"prompt_tokens_details":{}}}',
                "data: [DONE]",
            )
        )
        self.addAsyncCleanup(client.aclose)

        events = await _collect(provider, [Message(role="user", content="hello")])

        self.assertEqual(
            events,
            [TextDelta("a"), TokenUsage(1, 1, 2, 1), Finish("stop")],
        )

    async def test_rejects_stream_without_done_marker(self) -> None:
        provider, client = _provider_for(
            _sse_response(
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            )
        )
        self.addAsyncCleanup(client.aclose)

        with self.assertRaisesRegex(ProviderResponseError, r"before the \[DONE\]"):
            await _collect(provider, [Message(role="user", content="hello")])

    async def test_rejects_done_marker_without_finish_reason(self) -> None:
        provider, client = _provider_for(
            _sse_response(
                'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}',
                "data: [DONE]",
            )
        )
        self.addAsyncCleanup(client.aclose)

        with self.assertRaisesRegex(ProviderResponseError, "without a finish reason"):
            await _collect(provider, [Message(role="user", content="hello")])

    async def test_aggregates_split_tool_call_and_sends_schema(self) -> None:
        captured_body: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content))
            return _sse_response(
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"weather","arguments":"{\\"city\\":"}}]},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"深圳\\"}"}}]},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
                "data: [DONE]",
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = KimiProvider(KimiConfig(api_key="test-key"), client=client)
        definition = ToolDefinition(
            name="weather",
            description="Read weather.",
            input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
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
            [ToolCall("call-1", "weather", {"city": "深圳"}), Finish("tool_calls")],
        )
        tools = captured_body["tools"]
        assert isinstance(tools, list)
        self.assertEqual(tools[0]["function"]["name"], "weather")

    async def test_k26_replays_only_current_tool_loop_reasoning_by_default(self) -> None:
        captured_bodies: list[dict[str, object]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            captured_bodies.append(json.loads(request.content))
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
                thinking="当前思考",
                tool_calls=(ToolCall("call-1", "weather", {"city": "深圳"}),),
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

        await _collect(
            KimiProvider(KimiConfig(api_key="test-key"), client=client),
            history,
            [],
        )
        await _collect(
            KimiProvider(
                KimiConfig(api_key="test-key", thinking_keep="all"),
                client=client,
            ),
            history,
            [],
        )

        default_messages = captured_bodies[0]["messages"]
        keep_all_messages = captured_bodies[1]["messages"]
        assert isinstance(default_messages, list)
        assert isinstance(keep_all_messages, list)
        self.assertNotIn("reasoning_content", default_messages[1])
        self.assertEqual(default_messages[3]["reasoning_content"], "当前思考")
        self.assertEqual(keep_all_messages[1]["reasoning_content"], "旧思考")

    async def test_sends_k3_reasoning_effort(self) -> None:
        captured_body: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content))
            return _sse_response(
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = KimiProvider(
            KimiConfig(api_key="test-key", model="kimi-k3", reasoning_effort="high"),
            client=client,
        )

        await _collect(provider, [Message(role="user", content="hello")])

        self.assertEqual(captured_body["reasoning_effort"], "high")
        self.assertNotIn("thinking", captured_body)

    def test_validates_model_specific_thinking_capabilities(self) -> None:
        invalid = (
            ({"model": "kimi-k3", "thinking": "enabled"}, "does not accept"),
            ({"model": "kimi-k2.7-code", "thinking": "disabled"}, "cannot be disabled"),
            (
                {"model": "kimi-k2.7-code", "reasoning_effort": "high"},
                "does not support",
            ),
            ({"model": "kimi-k2.6", "reasoning_effort": "high"}, "does not support"),
            (
                {"model": "kimi-k2.6", "thinking": "disabled", "thinking_keep": "all"},
                "requires thinking",
            ),
            ({"model": "future-kimi", "thinking": "enabled"}, "unknown Kimi"),
        )
        for kwargs, message in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ConfigurationError, message):
                    KimiConfig(api_key="secret", **kwargs)  # type: ignore[arg-type]

        self.assertEqual(
            KimiConfig(api_key="secret", model="kimi-k2.7-code").model,
            "kimi-k2.7-code",
        )
        self.assertEqual(
            KimiConfig(api_key="secret", model="kimi-k2.6", thinking_keep="all").thinking_keep,
            "all",
        )

    async def test_maps_auth_quota_rate_limit_and_timeout_errors(self) -> None:
        cases = (
            (
                401,
                {"error": {"message": "invalid authentication"}},
                ProviderAuthenticationError,
            ),
            (
                429,
                {
                    "error": {
                        "type": "exceeded_current_quota_error",
                        "message": "quota exceeded",
                    }
                },
                ProviderBalanceError,
            ),
            (
                429,
                {"error": {"type": "rate_limit_error", "message": "slow down"}},
                ProviderRateLimitError,
            ),
        )
        for status, body, error_type in cases:
            with self.subTest(status=status, body=body):
                provider, client = _provider_for(httpx.Response(status, json=body))
                try:
                    with self.assertRaises(error_type) as raised:
                        await _collect(
                            provider,
                            [Message(role="user", content="hello")],
                        )
                    self.assertNotIn("super-secret", str(raised.exception))
                finally:
                    await client.aclose()

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        self.addAsyncCleanup(client.aclose)
        provider = KimiProvider(
            KimiConfig(api_key="super-secret", timeout_seconds=3),
            client=client,
        )
        with self.assertRaisesRegex(ProviderTimeoutError, "timed out after 3s"):
            await _collect(provider, [Message(role="user", content="hello")])

    async def test_task_cancellation_propagates_without_wrapping(self) -> None:
        stream_reached_blocking_point = asyncio.Event()

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[override]
                yield (
                    b'data: {"choices":[{"delta":{"content":"a"},'
                    b'"finish_reason":null}]}\n\n'
                )
                stream_reached_blocking_point.set()
                await asyncio.Event().wait()

        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=BlockingStream(),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = KimiProvider(KimiConfig(api_key="test-key"), client=client)
        task = asyncio.create_task(
            _collect(provider, [Message(role="user", content="hello")])
        )
        await asyncio.wait_for(stream_reached_blocking_point.wait(), timeout=1)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_provider_close_does_not_close_external_client(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
        self.addAsyncCleanup(client.aclose)
        provider = KimiProvider(KimiConfig(api_key="test-key"), client=client)

        await provider.aclose()
        await provider.aclose()

        self.assertFalse(client.is_closed)
        with self.assertRaisesRegex(ProviderError, "closed"):
            await _collect(provider, [Message(role="user", content="hello")])


def _sse_response(*events: str) -> httpx.Response:
    body = "\n\n".join(events) + "\n\n"
    return httpx.Response(
        200,
        headers={"Content-Type": "Text/Event-Stream; charset=utf-8"},
        content=body.encode(),
    )


def _provider_for(response: httpx.Response) -> tuple[KimiProvider, httpx.AsyncClient]:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    provider = KimiProvider(KimiConfig(api_key="super-secret"), client=client)
    return provider, client


async def _collect(
    provider: KimiProvider,
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
        async for event in provider.stream(
            messages,
            tools or [],
            context=context,
        )
    ]


if __name__ == "__main__":
    unittest.main()
