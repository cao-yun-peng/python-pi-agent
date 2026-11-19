"""SDK DTO boundary tests with scripted sessions; no process or network."""

import importlib.util
import io
import logging
import unittest

from pi_agent_py.tools.mcp._logging import sdk_log_privacy
from pi_agent_py.tools.mcp.contracts import (
    McpLimits,
    McpProtocolError,
    McpRequestNotStartedError,
)
from pi_agent_py.tools.mcp.sdk import McpSdkOptions, _SdkEndpoint


class Session:
    protocol_version = "2026-07-28"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def send_request(self, request, result_type):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    async def call_tool(self, *args, **kwargs):
        raise AssertionError("high-level call must never be used")

    async def list_tools(self, *args, **kwargs):
        raise AssertionError("mutating listing helper must never be used")


@unittest.skipUnless(importlib.util.find_spec("mcp"), "install [mcp]")
class McpSdkTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_returns_original_schema_and_complete_directory(self):
        from mcp_types import ListToolsResult

        session = Session(
            [
                ListToolsResult.model_validate(
                    {
                        "tools": [{"name": "one", "inputSchema": {"type": "object"}}],
                        "nextCursor": "next",
                    }
                ),
                ListToolsResult.model_validate(
                    {
                        "tools": [
                            {
                                "name": "two",
                                "inputSchema": {
                                    "type": "object",
                                    "x-mcp-header": "disallowed",
                                },
                            }
                        ]
                    }
                ),
            ]
        )
        endpoint = _SdkEndpoint(session, McpSdkOptions())
        tools = await endpoint.list_tools()
        self.assertEqual([t.remote_name for t in tools], ["one", "two"])
        self.assertEqual(tools[1].input_schema["x-mcp-header"], "disallowed")
        self.assertEqual(session.requests[1].params.cursor, "next")

    async def test_discovery_rejects_cursor_cycles_counts_pages_and_budget(self):
        from mcp_types import ListToolsResult

        def page(name, cursor=None):
            return ListToolsResult.model_validate(
                {
                    "tools": [{"name": name, "inputSchema": {"type": "object"}}],
                    "nextCursor": cursor,
                }
            )

        for pages, options in (
            ([page("one", "same"), page("two", "same")], McpSdkOptions()),
            ([page("one", "next"), page("one")], McpSdkOptions()),
            ([page("one", "next")], McpSdkOptions(max_discovery_pages=1)),
            (
                [page("one", "next"), page("two")],
                McpSdkOptions(limits=McpLimits(max_discovered_tools=1)),
            ),
            ([page("one")], McpSdkOptions(limits=McpLimits(max_catalog_bytes=1))),
        ):
            with self.assertRaises(McpProtocolError):
                await _SdkEndpoint(Session(pages), options).list_tools()

    async def test_presence_nontext_and_input_required_are_normalized_without_retries(
        self,
    ):
        from mcp_types import CallToolResult, InputRequiredResult

        for body, present in (
            ({"content": []}, False),
            ({"content": [], "structuredContent": None}, True),
        ):
            session = Session([CallToolResult.model_validate(body)])
            result = await _SdkEndpoint(session, McpSdkOptions()).call_tool("one", {})
            self.assertEqual(result.has_structured_content, present)
            self.assertEqual(len(session.requests), 1)
        session = Session(
            [
                CallToolResult.model_validate(
                    {
                        "content": [
                            {
                                "type": "image",
                                "mimeType": "image/png",
                                "data": "private-base64",
                            }
                        ]
                    }
                )
            ]
        )
        result = await _SdkEndpoint(session, McpSdkOptions()).call_tool("one", {})
        self.assertEqual(result.content[0].kind, "image")
        self.assertNotIn("private", repr(result))
        required = InputRequiredResult.model_validate(
            {
                "resultType": "input_required",
                "inputRequests": {},
                "requestState": "private",
            }
        )
        session = Session([required])
        result = await _SdkEndpoint(session, McpSdkOptions()).call_tool("one", {})
        self.assertEqual(result.result_type, "input_required")
        self.assertEqual(len(session.requests), 1)

    async def test_stale_protocol_error_preserves_cause_invalidates_and_never_resends(
        self,
    ):
        from mcp.shared.exceptions import MCPError
        from mcp_types import HEADER_MISMATCH

        original = MCPError(HEADER_MISMATCH, "private message")
        session = Session([original])
        endpoint = _SdkEndpoint(session, McpSdkOptions())
        with self.assertRaises(McpProtocolError) as caught:
            await endpoint.call_tool("one", {})
        self.assertIs(caught.exception.__cause__, original)
        with self.assertRaises(McpRequestNotStartedError):
            await endpoint.call_tool("one", {})
        self.assertEqual(len(session.requests), 1)

    async def test_received_list_change_invalidates_snapshot(self):
        from mcp_types import ToolListChangedNotification

        endpoint = _SdkEndpoint(Session([]), McpSdkOptions())
        await endpoint.on_message(ToolListChangedNotification())
        with self.assertRaises(McpRequestNotStartedError):
            await endpoint.call_tool("one", {})

    async def test_http_fault_keeps_original_transport_cause(self):
        from mcp.shared.exceptions import MCPError
        from pi_agent_py.tools.mcp.contracts import McpTransportError

        original = OSError("private transport diagnostic")
        fault = McpTransportError("MCP HTTP request lost its transport")
        fault.__cause__ = original
        session = Session([MCPError(-32603, "fixed SDK error")])
        endpoint = _SdkEndpoint(
            session,
            McpSdkOptions(),
            transport_failure=lambda: fault if session.requests else None,
        )
        with self.assertRaises(McpTransportError) as caught:
            await endpoint.call_tool("one", {})
        self.assertIs(caught.exception.__cause__, original)


class McpLoggingTests(unittest.TestCase):
    def test_sdk_raw_diagnostics_are_task_scoped_and_filters_are_released(self):
        logger = logging.getLogger("mcp.client.stdio")
        previous = tuple(logger.filters)
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger.addHandler(handler)
        try:
            with sdk_log_privacy():
                logger.error("private SDK exception/body")
            logger.error("ordinary external diagnostic")
            self.assertNotIn("private", output.getvalue())
            self.assertIn("ordinary external", output.getvalue())
            self.assertEqual(tuple(logger.filters), previous)
        finally:
            logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
