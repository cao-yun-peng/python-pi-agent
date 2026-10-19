from __future__ import annotations

import asyncio
import os

from pi_agent_py import Agent, AgentEvent, CallbackEventSink
from pi_agent_py.providers import DeepSeekConfig, DeepSeekProvider, ThinkingMode


def require_api_key() -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit(
            "请先设置 DEEPSEEK_API_KEY；密钥只从环境变量读取，不要写进源码。"
        )
    return api_key


async def main() -> None:
    raw_thinking = os.environ.get("DEEPSEEK_THINKING", "disabled")
    if raw_thinking not in {"enabled", "disabled"}:
        raise SystemExit("DEEPSEEK_THINKING 只能是 enabled 或 disabled")
    thinking: ThinkingMode = "enabled" if raw_thinking == "enabled" else "disabled"

    def observe(event: AgentEvent) -> None:
        if event.kind == "text_delta":
            print(event.payload["text"], end="", flush=True)

    config = DeepSeekConfig(
        api_key=require_api_key(),
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        thinking=thinking,
    )

    async with DeepSeekProvider(config) as provider:
        agent = Agent(
            provider,
            event_sink=CallbackEventSink(observe),
            system_prompt="你是一个简洁、可靠的中文助手。",
        )
        print("DeepSeek Agent 已启动，输入 /exit 退出。")
        while True:
            prompt = (await asyncio.to_thread(input, "\n你> ")).strip()
            if prompt in {"/exit", "/quit"}:
                break
            if not prompt:
                continue
            print("助手> ", end="", flush=True)
            result = await agent.run(prompt)
            if result.usage is not None:
                print(
                    f"\n[tokens: {result.usage.input_tokens} + "
                    f"{result.usage.output_tokens} = {result.usage.total_tokens}]"
                )


if __name__ == "__main__":
    asyncio.run(main())
