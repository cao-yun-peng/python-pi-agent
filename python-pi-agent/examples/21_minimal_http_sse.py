"""最小可运行示例：不经过 Agent 框架，直接观察 DeepSeek 的 HTTP 与 SSE。

这个脚本只做四件事：

    构造请求 -> 发送请求 -> 打印状态码/Content-Type -> 逐条读取 SSE 并解析 JSON

它刻意不导入 ``pi_agent_py``。目的是把三层知识分开：

1. HTTP 知识：方法、URL、Header、状态码、Content-Type；
2. DeepSeek 协议：``choices[].delta``、``finish_reason``、``usage``、``[DONE]``；
3. 项目抽象：``DeepSeekProvider`` 如何把 1、2 转换成 ``ModelStreamEvent``。

默认使用 ``examples/fixtures/`` 中的脱敏 fixture，通过 ``httpx.MockTransport``
走完整的 HTTP 客户端路径，但不访问网络、不消耗额度。

用法::

    python 21_minimal_http_sse.py                     # 离线流式 fixture
    python 21_minimal_http_sse.py --mode non-stream   # 离线非流式 fixture
    python 21_minimal_http_sse.py --fixture tool-call # 离线工具调用分片 fixture
    python 21_minimal_http_sse.py --live --prompt "你好"   # 真实调用

真实调用需要环境变量 ``DEEPSEEK_API_KEY``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
BASE_URL = "https://api.deepseek.com"
SYSTEM_PROMPT = "你是一个简洁、可靠的中文助手。"


def mask_secret(value: str) -> str:
    """只保留可排查的前缀和后四位，避免把密钥打进日志。"""
    if len(value) <= 12:
        return "***"
    return f"{value[:7]}...{value[-4:]}"


def build_payload(prompt: str, *, model: str, stream: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": stream,
        "thinking": {"type": "disabled"},
    }
    if stream:
        # 让最后一个 chunk 带上 usage；否则流式响应默认不返回 token 统计。
        payload["stream_options"] = {"include_usage": True}
    return payload


def iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    """按 SSE 规范把字节流切成一条条 data 字段。

    - 空行代表一个事件结束；
    - 以 ``:`` 开头的是注释 / keep-alive，直接忽略；
    - 以 ``data:`` 开头的行才承载 JSON；
    - 一个事件可以有多行 data，用换行拼接。
    """
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def load_fixture(name: str, *, stream: bool) -> tuple[bytes, str]:
    if not stream:
        return (FIXTURE_DIR / "deepseek_non_stream.json").read_bytes(), "application/json"
    filename = (
        "deepseek_stream_tool_call.sse"
        if name == "tool-call"
        else "deepseek_stream_text.sse"
    )
    return (FIXTURE_DIR / filename).read_bytes(), "text/event-stream"


def offline_transport(fixture: str, *, stream: bool) -> httpx.MockTransport:
    body, content_type = load_fixture(fixture, stream=stream)

    def handle(request: httpx.Request) -> httpx.Response:
        # 这里可以检查 request.url / headers / content，证明请求确实被构造出来了。
        assert request.method == "POST"
        assert request.url.path == "/chat/completions"
        return httpx.Response(
            200,
            headers={"Content-Type": content_type},
            content=body,
        )

    return httpx.MockTransport(handle)


def print_request(request: httpx.Request) -> None:
    print("=" * 72)
    print(f"请求方法: {request.method}")
    print(f"请求 URL : {request.url}")
    print("请求头   :")
    for key, value in request.headers.items():
        if key.lower() == "authorization":
            scheme, _, token = value.partition(" ")
            value = f"{scheme} {mask_secret(token)}"
        print(f"  {key}: {value}")
    print("请求体   :")
    print(json.dumps(json.loads(request.content), ensure_ascii=False, indent=2))


def print_non_stream_response(response: httpx.Response) -> None:
    print("=" * 72)
    print(f"状态码      : {response.status_code}")
    print(f"Content-Type: {response.headers.get('Content-Type')}")
    payload = response.json()
    print("完整响应体  :")
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    choice = payload["choices"][0]
    message = choice["message"]
    print("-" * 72)
    print(f"finish_reason      : {choice['finish_reason']}")
    print(f"message.content    : {message.get('content')!r}")
    print(f"reasoning_content  : {message.get('reasoning_content')!r}")
    usage = payload.get("usage") or {}
    print(
        "usage              : "
        f"{usage.get('prompt_tokens')} + {usage.get('completion_tokens')} "
        f"= {usage.get('total_tokens')}"
    )


def print_stream_response(response: httpx.Response, *, fixture: str) -> None:
    print("=" * 72)
    print(f"状态码      : {response.status_code}")
    print(f"Content-Type: {response.headers.get('Content-Type')}")
    if "text/event-stream" not in response.headers.get("Content-Type", ""):
        raise SystemExit("期望 text/event-stream，但服务端返回了别的类型")

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_argument_parts: dict[int, list[str]] = {}

    print("逐条 SSE 事件:")
    for index, data in enumerate(iter_sse_data(response.iter_lines()), start=1):
        if data == "[DONE]":
            print(f"  [{index:02d}] [DONE]  <- 流结束标记，不是 JSON")
            break

        chunk = json.loads(data)
        choices = chunk.get("choices") or []
        delta = choices[0].get("delta", {}) if choices else {}
        finish_reason = choices[0].get("finish_reason") if choices else None

        pieces: list[str] = []
        if delta.get("reasoning_content"):
            thinking_parts.append(delta["reasoning_content"])
            pieces.append(f"reasoning_content={delta['reasoning_content']!r}")
        if delta.get("content"):
            text_parts.append(delta["content"])
            pieces.append(f"content={delta['content']!r}")
        for raw_call in delta.get("tool_calls") or []:
            index_ = raw_call.get("index", 0)
            arguments = (raw_call.get("function") or {}).get("arguments")
            if arguments is not None:
                tool_argument_parts.setdefault(index_, []).append(arguments)
                pieces.append(f"tool_calls[{index_}].arguments={arguments!r}")
            name = (raw_call.get("function") or {}).get("name")
            if name:
                pieces.append(f"tool_calls[{index_}].function.name={name!r}")
        if finish_reason:
            pieces.append(f"finish_reason={finish_reason!r}")
        if chunk.get("usage"):
            pieces.append(f"usage={chunk['usage']}")
        if not pieces:
            pieces.append("空 delta（例如角色声明或纯心跳）")

        print(f"  [{index:02d}] " + " | ".join(pieces))

    print("-" * 72)
    print(f"拼接后的 thinking: {''.join(thinking_parts)!r}")
    print(f"拼接后的 content : {''.join(text_parts)!r}")
    if tool_argument_parts:
        print("拼接后的工具参数 :")
        for index_, parts in sorted(tool_argument_parts.items()):
            joined = "".join(parts)
            print(f"  tool_calls[{index_}] 原始分片: {parts}")
            print(f"  tool_calls[{index_}] 拼接结果: {joined}")
            print(f"  tool_calls[{index_}] 解析结果: {json.loads(joined)}")
    if fixture == "tool-call" and not tool_argument_parts:
        raise SystemExit("tool-call fixture 没有产生工具参数分片")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("stream", "non-stream"),
        default="stream",
        help="stream 使用 SSE；non-stream 使用一次性 JSON",
    )
    parser.add_argument(
        "--fixture",
        choices=("text", "tool-call"),
        default="text",
        help="离线流式模式下使用的 fixture",
    )
    parser.add_argument("--prompt", default="你好")
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实请求 DeepSeek；默认离线使用 MockTransport + fixture",
    )
    args = parser.parse_args()

    stream = args.mode == "stream"
    payload = build_payload(args.prompt, model=args.model, stream=stream)

    if args.live:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise SystemExit(
                "真实调用需要先设置 DEEPSEEK_API_KEY；不加 --live 则使用离线 fixture。"
            )
        transport = None
        print("传输方式: 真实 HTTPS 请求")
    else:
        api_key = "sk-offline-fixture-key"
        transport = offline_transport(args.fixture, stream=stream)
        print("传输方式: httpx.MockTransport + 脱敏 fixture（不访问网络）")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    url = f"{BASE_URL}/chat/completions"

    with httpx.Client(transport=transport, timeout=60.0) as client:
        request = client.build_request("POST", url, headers=headers, json=payload)
        print_request(request)
        response = client.send(request, stream=stream)
        try:
            if stream:
                print_stream_response(response, fixture=args.fixture)
            else:
                print_non_stream_response(response)
        finally:
            response.close()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as error:
        print(f"HTTP 层失败: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
