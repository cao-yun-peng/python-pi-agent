"""Small provider-neutral Server-Sent Events framing helper."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx


async def iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    """Yield complete SSE event/data pairs, ignoring comments and keep-alives."""
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = None
            data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        yield event_name, "\n".join(data_lines)
