"""Run two offline prompts and show a cancelled run's terminal events."""

import asyncio
from decimal import Decimal

from pi_agent_py.api import (
    AgentRuntimeBuilder,
    Finish,
    ModelPrice,
    PricingTable,
    TextDelta,
    TokenUsage,
)
from pi_agent_py.testing import FakeProvider, WaitUntilCancelled


async def main() -> None:
    provider = FakeProvider(
        [
            [TextDelta("first"), TokenUsage(100, 10, 110, 20), Finish()],
            [TextDelta("follow-up"), TokenUsage(80, 5, 85), Finish()],
            [WaitUntilCancelled()],
        ]
    )
    pricing = PricingTable(
        {
            "demo": ModelPrice(Decimal("2"), Decimal("5"), Decimal("0.5")),
        }
    )
    runtime = await (
        AgentRuntimeBuilder()
        .with_provider(provider, model="demo")
        .with_pricing(pricing)
        .build()
    )
    async with runtime, await runtime.open_session("demo") as session:
        first = await session.submit("Start the example.")
        follow_up = await session.follow_up("Continue.")
        for handle in (first, follow_up):
            async with handle.events() as events:
                async for event in events:
                    print(handle.run_id[:8], event.kind)
            print("result:", (await handle.result()).message.content)
        print("session usage:", session.usage)

        cancelled = await session.submit("Wait.")
        await provider.blocked.wait()
        await cancelled.abort()
        print("last events:", [event.kind async for event in cancelled.events()][-2:])
        try:
            await cancelled.result()
        except asyncio.CancelledError:
            print("result: cancelled")


if __name__ == "__main__":
    asyncio.run(main())
