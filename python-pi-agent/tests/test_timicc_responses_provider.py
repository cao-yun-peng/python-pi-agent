from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from pi_agent_py import (
    ConfigurationError,
    Finish,
    Message,
    ProviderError,
    ProviderHTTPError,
    ProviderRequestContext,
    ProviderResponseError,
    ProviderState,
    ProviderTimeoutError,
    TextDelta,
    ThinkingDelta,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from pi_agent_py.providers import TimiccResponsesConfig, TimiccResponsesProvider


class TimiccResponsesProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_text_reasoning_usage_and_request(self) -> None:
        captured: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers["Authorization"]
            captured["body"] = json.loads(request.content)
            return _sse_response(
                _event("response.reasoning_summary_text.delta", delta="先想"),
                _event("response.output_text.delta", delta="你好"),
                _event(
                    "response.output_item.done",
                    item={
                        "id": "rs_1",
                        "type": "reasoning",
                        "encrypted_content": "opaque-ciphertext",
                        "summary": [],
                    },
                ),
                _completed(4, 2, 6, cached=1),
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        provider = TimiccResponsesProvider(
            TimiccResponsesConfig(
                api_key="test-key",
                model="gpt-6-astra",
                reasoning_effort="high",
                reasoning_summary="auto",
                max_output_tokens=2048,
            ),
            client=client,
        )

        events = await _collect(provider, [Message(role="user", content="你好")])

        self.assertEqual(events[:2], [ThinkingDelta("先想"), TextDelta("你好")])
        self.assertEqual(events[2].provider, "timicc-responses")
        self.assertEqual(events[2].item["encrypted_content"], "opaque-ciphertext")
        self.assertEqual(events[3:], [TokenUsage(4, 2, 6, 1), Finish("stop")])
        self.assertEqual(captured["url"], "https://timicc.com/v1/responses")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "gpt-6-astra")
        self.assertEqual(body["input"], [{"role": "user", "content": "你好"}])
        self.assertEqual(body["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertEqual(body["max_output_tokens"], 2048)
        self.assertIs(body["stream"], True)
        self.assertIs(body["store"], False)
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])

    async def test_aggregates_function_call_and_uses_flat_tool_shape(self) -> None:
        captured: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return _sse_response(
                _event(
                    "response.output_item.added",
                    output_index=0,
                    item={
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "weather",
                        "arguments": "",
                    },
                ),
                _event(
                    "response.function_call_arguments.delta",
                    item_id="fc_1",
                    output_index=0,
                    delta='{"city":',
                ),
                _event(
                    "response.function_call_arguments.delta",
                    item_id="fc_1",
                    output_index=0,
                    delta='"杭州"}',
                ),
                _event(
                    "response.output_item.done",
                    output_index=0,
                    item={
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "weather",
                        "arguments": '{"city":"杭州"}',
                        "status": "completed",
                    },
                ),
                _completed(10, 3, 13),
            )

        tool = ToolDefinition(
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
            TimiccResponsesProvider(
                TimiccResponsesConfig(api_key="key", model="grok-4.6"),
                client=client,
            ),
            [Message(role="user", content="天气")],
            [tool],
        )

        self.assertEqual(events[0], ProviderState("timicc-responses", events[0].item))
        self.assertEqual(events[1], ToolCall("call_1", "weather", {"city": "杭州"}))
        self.assertEqual(events[-2:], [TokenUsage(10, 3, 13), Finish("tool_calls")])
        tools = captured["tools"]
        assert isinstance(tools, list)
        self.assertEqual(
            tools[0],
            {
                "type": "function",
                "name": "weather",
                "description": "Read weather.",
                "parameters": tool.input_schema,
                "strict": False,
            },
        )

    async def test_replays_opaque_items_and_function_output(self) -> None:
        captured: dict[str, object] = {}
        reasoning = {
            "id": "rs_1",
            "type": "reasoning",
            "encrypted_content": "ciphertext",
            "summary": [],
        }
        function_call = {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "weather",
            "arguments": '{"city":"杭州"}',
            "status": "completed",
        }

        def handle(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return _sse_response(_completed(1, 1, 2))

        history = [
            Message(role="user", content="天气"),
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall("call_1", "weather", {"city": "杭州"}),),
                provider_state=(
                    ProviderState("timicc-responses", reasoning),
                    ProviderState("timicc-responses", function_call),
                ),
            ),
            Message(
                role="tool",
                content="晴",
                tool_call_id="call_1",
                tool_name="weather",
            ),
        ]
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)

        await _collect(
            TimiccResponsesProvider(
                TimiccResponsesConfig(api_key="key", model="gpt-6-astra"),
                client=client,
            ),
            history,
        )

        self.assertEqual(
            captured["input"],
            [
                {"role": "user", "content": "天气"},
                reasoning,
                function_call,
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "晴",
                },
            ],
        )

    async def test_maps_http_incomplete_and_protocol_failures(self) -> None:
        provider, client = _provider_for(
            httpx.Response(503, json={"error": {"message": "busy"}})
        )
        try:
            with self.assertRaises(ProviderHTTPError) as raised:
                await _collect(provider, [Message(role="user", content="hi")])
            self.assertTrue(raised.exception.retryable)
            self.assertNotIn("super-secret", str(raised.exception))
        finally:
            await client.aclose()

        for response, message in (
            (
                _sse_response(
                    _event(
                        "response.incomplete",
                        response={
                            "incomplete_details": {"reason": "max_output_tokens"},
                            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                        },
                    )
                ),
                None,
            ),
            (_sse_response(_event("response.output_text.delta", delta="partial")), "terminal"),
        ):
            provider, client = _provider_for(response)
            try:
                if message is None:
                    self.assertEqual(
                        await _collect(provider, [Message(role="user", content="hi")]),
                        [TokenUsage(1, 2, 3), Finish("length")],
                    )
                else:
                    with self.assertRaisesRegex(ProviderResponseError, message):
                        await _collect(provider, [Message(role="user", content="hi")])
            finally:
                await client.aclose()

    async def test_timeout_and_config_validation(self) -> None:
        for kwargs, message in (
            ({"model": ""}, "model"),
            ({"max_output_tokens": 15}, "at least 16"),
            ({"reasoning_effort": "extreme"}, "reasoning_effort"),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ConfigurationError, message):
                    options: dict[str, object] = {"model": "gpt-5", **kwargs}
                    TimiccResponsesConfig(api_key="key", **options)  # type: ignore[arg-type]

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        self.addAsyncCleanup(client.aclose)
        provider = TimiccResponsesProvider(
            TimiccResponsesConfig(
                api_key="key",
                model="gpt-6-astra",
                timeout_seconds=3,
            ),
            client=client,
        )
        with self.assertRaisesRegex(ProviderTimeoutError, "3s"):
            await _collect(provider, [Message(role="user", content="hi")])

    async def test_cancellation_and_external_client_lifecycle(self) -> None:
        reached = asyncio.Event()

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[override]
                yield (
                    _event("response.output_text.delta", delta="partial")
                    + "\n\n"
                ).encode()
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
        provider = TimiccResponsesProvider(
            TimiccResponsesConfig(api_key="key", model="gpt-6-astra"),
            client=client,
        )
        task = asyncio.create_task(
            _collect(provider, [Message(role="user", content="hi")])
        )
        await asyncio.wait_for(reached.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        await provider.aclose()
        self.assertFalse(client.is_closed)
        with self.assertRaisesRegex(ProviderError, "closed"):
            await _collect(provider, [Message(role="user", content="hi")])


def _event(event_type: str, **values: object) -> str:
    return "event: " + event_type + "\ndata: " + json.dumps(
        {"type": event_type, **values}, separators=(",", ":")
    )


def _completed(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    *,
    cached: int | None = None,
) -> str:
    usage: dict[str, object] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if cached is not None:
        usage["input_tokens_details"] = {"cached_tokens": cached}
    return _event("response.completed", response={"usage": usage})


def _sse_response(*events: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        content=("\n\n".join(events) + "\n\n").encode(),
    )


def _provider_for(
    response: httpx.Response,
) -> tuple[TimiccResponsesProvider, httpx.AsyncClient]:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return (
        TimiccResponsesProvider(
            TimiccResponsesConfig(api_key="super-secret", model="gpt-6-astra"),
            client=client,
        ),
        client,
    )


async def _collect(
    provider: TimiccResponsesProvider,
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
