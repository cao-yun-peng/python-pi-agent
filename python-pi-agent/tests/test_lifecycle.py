from __future__ import annotations

import unittest

from pi_agent_py import (
    LifecycleScope,
    ManagedResource,
    RuntimeCloseError,
)


class _Resource:
    def __init__(
        self,
        name: str,
        trace: list[str],
        *,
        failure: Exception | None = None,
    ) -> None:
        self._name = name
        self._trace = trace
        self._failure = failure

    async def aclose(self) -> None:
        self._trace.append(self._name)
        if self._failure is not None:
            raise self._failure


class LifecycleScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_closes_owned_resources_in_reverse_and_skips_borrowed(self) -> None:
        trace: list[str] = []
        scope = LifecycleScope(name="test")
        borrowed = _Resource("borrowed", trace)
        first = _Resource("first", trace)
        second = _Resource("second", trace)

        self.assertIs(
            scope.manage(ManagedResource("borrowed", borrowed)),
            borrowed,
        )
        scope.manage(ManagedResource("first", first, ownership="owned"))
        scope.manage(ManagedResource("second", second, ownership="owned"))

        await scope.aclose()
        await scope.aclose()

        self.assertEqual(trace, ["second", "first"])

    async def test_attempts_every_cleanup_and_reports_bounded_types(self) -> None:
        trace: list[str] = []
        scope = LifecycleScope(name="test")
        scope.manage(
            ManagedResource(
                "first",
                _Resource("first", trace, failure=ValueError("private one")),
                ownership="owned",
            )
        )
        scope.manage(
            ManagedResource(
                "second",
                _Resource("second", trace, failure=OSError("private two")),
                ownership="owned",
            )
        )

        with self.assertRaises(RuntimeCloseError) as raised:
            await scope.aclose()

        self.assertEqual(trace, ["second", "first"])
        self.assertEqual(
            tuple(type(error) for error in raised.exception.failures),
            (OSError, ValueError),
        )
        self.assertNotIn("private", str(raised.exception))

    def test_borrowed_resource_cannot_smuggle_a_cleanup(self) -> None:
        with self.assertRaisesRegex(ValueError, "borrowed resources"):
            ManagedResource(
                "borrowed",
                object(),
                cleanup=lambda: None,
            )


if __name__ == "__main__":
    unittest.main()
