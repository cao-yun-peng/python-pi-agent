"""HTTP budget and policy contracts, using byte streams rather than sockets."""

import importlib.util
import unittest

from pi_agent_py.errors import ConfigurationError
from pi_agent_py.tools.mcp.contracts import McpConnectionSpec

if importlib.util.find_spec("httpx2") is None:
    raise unittest.SkipTest("install [mcp]")
import httpx2
from pi_agent_py.tools.mcp._http import BoundedHttpTransport, validate_http_target
from pi_agent_py.tools.mcp.contracts import (
    McpAuthenticationError,
    McpProtocolError,
    McpResultError,
    McpTransportError,
)


class Stream(httpx2.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class HttpGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_and_sse_are_bounded_before_parse_and_streams_close(self):
        for content_type in ("application/json", "text/event-stream"):
            stream = Stream((b"1234", b"5678", b"not read"))

            def handler(request):
                self.assertEqual(request.headers["accept-encoding"], "identity")
                return httpx2.Response(
                    200, headers={"content-type": content_type}, stream=stream
                )

            guarded = BoundedHttpTransport(httpx2.MockTransport(handler), max_bytes=5)
            async with httpx2.AsyncClient(transport=guarded) as client:
                with self.assertRaises(httpx2.StreamError):
                    await client.get("https://example.test/mcp")
            self.assertEqual(stream.reads, 2)
            self.assertTrue(stream.closed)
            self.assertIsInstance(guarded.failure, McpResultError)

    async def test_auth_redirect_encoding_and_declared_size_reject_without_body_read(
        self,
    ):
        for status, headers, error in (
            (401, {}, McpAuthenticationError),
            (307, {"location": "https://elsewhere.test/mcp"}, McpTransportError),
            (200, {"content-encoding": "gzip"}, McpProtocolError),
            (200, {"content-length": "100"}, McpResultError),
        ):
            stream = Stream((b"private response body",))
            guarded = BoundedHttpTransport(
                httpx2.MockTransport(
                    lambda request: httpx2.Response(
                        status, headers=headers, stream=stream
                    )
                ),
                max_bytes=10,
            )
            async with httpx2.AsyncClient(transport=guarded) as client:
                response = await client.get("https://example.test/mcp")
                self.assertEqual(response.status_code, 400)
            self.assertIsInstance(guarded.failure, error)
            self.assertEqual(stream.reads, 0)
            self.assertTrue(stream.closed)

    async def test_resumption_is_rejected_before_network(self):
        def forbidden(request):
            self.fail("must not submit a resumption request")

        async with httpx2.AsyncClient(
            transport=BoundedHttpTransport(
                httpx2.MockTransport(forbidden), max_bytes=10
            )
        ) as client:
            response = await client.get(
                "https://example.test/mcp", headers={"last-event-id": "private"}
            )
            self.assertEqual(response.status_code, 400)

    def test_http_requires_explicit_loopback_permission_and_safe_headers(self):
        spec = McpConnectionSpec("local", "streamable_http", url="http://127.0.0.1/mcp")
        with self.assertRaises(ConfigurationError):
            validate_http_target(spec, allow_insecure_local=False)
        validate_http_target(spec, allow_insecure_local=True)
        for url in ("http://example.test/mcp", "http://127.0.0.1/mcp\n"):
            with self.assertRaises(ConfigurationError):
                validate_http_target(
                    McpConnectionSpec("bad", "streamable_http", url=url),
                    allow_insecure_local=True,
                )
        with self.assertRaises(ConfigurationError):
            validate_http_target(
                McpConnectionSpec(
                    "bad",
                    "streamable_http",
                    url="https://example.test/mcp",
                    headers={"Mcp-Session-Id": "private"},
                ),
                allow_insecure_local=False,
            )


if __name__ == "__main__":
    unittest.main()
