"""Owner lifecycle contracts independent of the optional SDK and transport."""

import asyncio
import unittest
from contextlib import asynccontextmanager

from pi_agent_py.testing import FakeMcpEndpoint, WaitUntilCancelled
from pi_agent_py.tools.mcp.connection import OwnedMcpEndpoint
from pi_agent_py.tools.mcp.contracts import (
    McpCallResult,
    McpContent,
    McpRequestNotStartedError,
    McpTransportError,
)


class McpOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_errors_are_reported_and_cannot_leave_owner_alive(self):
        @asynccontextmanager
        async def factory():
            yield FakeMcpEndpoint(())
            raise ValueError("private cleanup error")

        from pi_agent_py.tools.mcp.contracts import McpConnectionError

        endpoint = await OwnedMcpEndpoint.open(factory)
        with self.assertRaises(McpConnectionError) as caught:
            await endpoint.aclose()
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(endpoint.state, "closed")

    async def test_enter_and_exit_share_owner_across_caller_tasks(self):
        tasks = []
        backend = FakeMcpEndpoint((), (McpCallResult((McpContent("text", "ok"),)),))

        @asynccontextmanager
        async def factory():
            tasks.append(asyncio.current_task())
            try:
                yield backend
            finally:
                tasks.append(asyncio.current_task())
                await backend.aclose()

        endpoint = await asyncio.create_task(OwnedMcpEndpoint.open(factory))
        result = await asyncio.create_task(endpoint.call_tool("echo", {}))
        self.assertEqual(result.content[0].text, "ok")
        await asyncio.create_task(endpoint.aclose())
        await endpoint.aclose()
        self.assertIs(tasks[0], tasks[1])
        self.assertIsNot(tasks[0], asyncio.current_task())
        self.assertEqual(endpoint.state, "closed")
        self.assertEqual(backend.close_count, 1)

    async def test_call_cancel_releases_slot_and_preserves_other_waiter(self):
        backend = FakeMcpEndpoint(
            (), (WaitUntilCancelled(), McpCallResult((McpContent("text", "next"),)))
        )

        @asynccontextmanager
        async def factory():
            try:
                yield backend
            finally:
                await backend.aclose()

        endpoint = await OwnedMcpEndpoint.open(factory, max_pending_requests=1)
        first = asyncio.create_task(endpoint.call_tool("one", {}))
        await backend.blocked.wait()
        second = asyncio.create_task(endpoint.call_tool("two", {}))
        await asyncio.sleep(0)
        with self.assertRaises(McpRequestNotStartedError):
            await endpoint.call_tool("overflow", {})
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual((await second).content[0].text, "next")
        await endpoint.aclose()
        self.assertEqual([c[0] for c in backend.calls], ["one", "two"])

    async def test_cancel_before_ready_cleans_partial_startup(self):
        entered, released = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def factory():
            try:
                entered.set()
                await asyncio.Event().wait()
                yield FakeMcpEndpoint(())
            finally:
                released.set()

        task = asyncio.create_task(OwnedMcpEndpoint.open(factory))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(released.is_set())

    async def test_broken_connection_rejects_queued_and_new_calls_without_send(self):
        error = McpTransportError("private diagnostic")
        backend = FakeMcpEndpoint((), (error,))

        @asynccontextmanager
        async def factory():
            yield backend

        endpoint = await OwnedMcpEndpoint.open(factory)
        with self.assertRaises(McpTransportError) as caught:
            await endpoint.call_tool("one", {})
        self.assertIs(caught.exception, error)
        with self.assertRaises(McpRequestNotStartedError):
            await endpoint.call_tool("two", {})
        await endpoint.aclose()
        self.assertEqual(len(backend.calls), 1)


if __name__ == "__main__":
    unittest.main()
