# 第二章：从一次 HTTP 调用到可替换的 DeepSeekProvider

本章只回答一个问题：

> 一段用户输入，怎样变成一条 `assistant` 消息？中间哪些是 HTTP，哪些是 DeepSeek 协议，哪些才是这个项目自己的抽象？

不要一上来就读 `DeepSeekProvider`。先看清线路上真正传输的字节，再回头看代码，`Provider` 的存在才有意义。

## 本章主线

```text
用户消息
  ↓
HTTP 请求：headers + JSON body
  ↓
模型 API
  ↓
HTTP/SSE 原始响应
  ↓
供应商响应对象
  ↓
DeepSeekProvider 协议转换
  ↓
统一 ModelStreamEvent
  ↓
Agent Runtime 聚合
  ↓
最终 Message / ToolCall / TokenUsage
```

整章按这个顺序展开，共 16 节：

1. 模型 API 到底是什么
2. 一次完整 HTTP 请求
3. 非流式完整响应
4. 流式 SSE 完整响应
5. 手写最小 `httpx` 客户端
6. 为什么需要 Provider
7. `DeepSeekProvider` 请求构造
8. SSE 拆帧与 JSON 解析
9. 文本、thinking、usage 的标准化
10. 工具调用为什么需要分片累积
11. Runtime 如何消费统一事件
12. 使用 `MockTransport` 验证协议
13. 在完整 Agent 中运行
14. 对照开源 Pi 的生产级拆分
15. 独立实现第二个 Provider
16. 两个实现完成后再提取公共兼容层

---

## 1. 模型 API 到底是什么

把“大模型 API”还原成三个事实：

1. 它是一个**无状态 HTTP 接口**。服务端不记得上一轮对话，客户端每次都要把完整 `messages` 发过去。
2. 它返回的是**结构化文本**，不是函数调用，也不是对象。非流式返回一个 JSON，流式返回一串 SSE 文本帧。
3. 所谓“流式输出”，只是把同一个响应用很多个小 JSON 分片提前发回来，协议本身没有变。

一次请求真正需要的东西只有四样：

```text
POST https://api.deepseek.com/chat/completions
Authorization: Bearer <API_KEY>
Content-Type: application/json
{ "model": "...", "messages": [...], "stream": true }
```

其余字段都是可选的“行为开关”：工具 schema、思考模式、最大输出长度、token 统计等。

这里要先分清三层知识，否则后面容易把“DeepSeek 的字段”误认为“Agent 的字段”：

| 层次 | 例子 | 谁应该知道 |
|---|---|---|
| HTTP | 方法、URL、Header、状态码、`Content-Type` | Provider 的网络层 |
| 供应商协议 | `choices[].delta`、`reasoning_content`、`finish_reason`、`[DONE]` | Provider 的解析层 |
| 项目领域 | `TextDelta`、`ThinkingDelta`、`ToolCall`、`TokenUsage`、`Finish` | Runtime 与所有上层 |

本章的目标，就是把第三层从第一、二层里“救”出来。

---

## 2. 一次完整 HTTP 请求

以项目实际发送的请求为例。

### 2.1 请求行与请求头

```http
POST /chat/completions HTTP/1.1
Host: api.deepseek.com
Authorization: Bearer sk-********
Content-Type: application/json
Accept: text/event-stream
```

三个 Header 各有职责：

- `Authorization`：身份，不属于业务参数；
- `Content-Type`：告诉服务端请求体是 JSON；
- `Accept`：告诉服务端“我期望流式响应”。它只是声明，服务端仍可能返回普通 JSON，所以客户端必须自己校验 `Content-Type`。

### 2.2 完整请求体

```json
{
  "model": "deepseek-flash",
  "messages": [
    {
      "role": "system",
      "content": "你是一个简洁、可靠的中文助手。"
    },
    {
      "role": "user",
      "content": "你好"
    }
  ],
  "stream": true,
  "stream_options": {
    "include_usage": true
  },
  "thinking": {
    "type": "disabled"
  }
}
```

字段来源可以一一对上：

| 请求字段 | 来源 |
|---|---|
| `model` | `DeepSeekConfig.model` |
| `messages` | Runtime 传入的 `Sequence[Message]` |
| `stream` | 项目固定为 `true` |
| `stream_options.include_usage` | 为了在流末尾拿到 token 统计 |
| `thinking.type` | `DeepSeekConfig.thinking` |
| `tools` | `Sequence[ToolDefinition]`，没有工具时整个字段省略 |
| `max_tokens` / `reasoning_effort` / `user_id` | 对应配置，未设置时省略 |

注意两个工程细节：

- **可选字段不发送，而不是发送 `null`。** 有些供应商会把显式的 `null` 当成非法值。
- **工具为空时整个 `tools` 字段不出现。** 空数组和“不支持工具”在语义上并不相同。

### 2.3 从项目对象到请求字典

同样一条用户消息，在项目里是：

```python
Message(role="user", content="你好")
```

经过 `_serialize_message()` 之后变成：

```python
{"role": "user", "content": "你好"}
```

带工具调用的 assistant 消息则复杂一些：

```python
Message(
    role="assistant",
    content="",
    tool_calls=(ToolCall(id="call-1", name="weather", arguments={"city": "深圳"}),),
)
```

序列化为：

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call-1",
      "type": "function",
      "function": {
        "name": "weather",
        "arguments": "{\"city\":\"深圳\"}"
      }
    }
  ]
}
```

两个容易踩的点：

1. `arguments` 在供应商协议里是**字符串**，不是对象。项目内部是 `dict`，边界处必须 `json.dumps`。
2. `content` 为空时要发 `null`，不能发空字符串。空字符串在部分实现里会被当成“有内容但内容为空”。

---

## 3. 非流式完整响应

先看不流式时服务端返回什么。`stream: false` 时，响应体是一个完整 JSON：

```json
{
  "id": "chatcmpl-fixture-nonstream",
  "object": "chat.completion",
  "created": 1730000000,
  "model": "deepseek-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "你好，我是 DeepSeek。",
        "reasoning_content": "用户只是打招呼，简短回应即可。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 9,
    "total_tokens": 21,
    "prompt_tokens_details": {
      "cached_tokens": 0
    }
  }
}
```

只需要关心四个位置：

| 位置 | 含义 |
|---|---|
| `choices[0].message.content` | 面向用户的最终文本 |
| `choices[0].message.reasoning_content` | 思考内容（开启 thinking 时可能出现） |
| `choices[0].finish_reason` | 为什么结束：`stop` / `length` / `tool_calls` |
| `usage` | 输入、输出、总 token，以及缓存命中数 |

提取最终文本只要一行：

```python
text = payload["choices"][0]["message"]["content"]
```

但非流式的代价是：**必须等模型全部生成完，才能拿到第一个字。** 对聊天产品来说，这个等待不可接受，所以真实项目使用流式。

---

## 4. 流式 SSE 完整响应

### 4.1 两种“完整响应”的区别

| 类型 | 返回形式 | 主要字段 |
|---|---|---|
| `stream: false` | 一个完整 JSON 对象 | `choices[0].message.content`、`finish_reason`、`usage` |
| `stream: true` | 多条 SSE `data:` 消息 | `delta.content`、`delta.reasoning_content`、`delta.tool_calls`、最后的 `usage`、`[DONE]` |

本项目使用 `stream: true`。官方协议把它描述为若干 `chat.completion.chunk`，最后以 `data: [DONE]` 结束；工具参数也可能被拆到多个 chunk 中。

### 4.2 SSE 的四条规则

SSE（Server-Sent Events）本身是文本协议，和模型无关：

1. **空行表示一个事件结束。** 读到空行，才把前面攒的 `data:` 行拼成一个事件。
2. **只有 `data:` 后面才是数据。** 前缀可能是 `data:`，也可能是 `data: `，都要兼容。
3. **以 `:` 开头的是注释或 keep-alive。** 直接丢弃，不要当 JSON 解析。
4. **一个事件可以有多行 `data:`。** 按规范应该用换行拼接，而不是只取第一行。

此外还有两个模型协议层的约定：

- `data: [DONE]` 是结束标记，**不是 JSON**；
- `usage` 通常出现在最后一个 chunk，此时 `choices` 可能是空数组。

### 4.3 一份完整 transcript

下面这份记录来自 `examples/fixtures/deepseek_stream_text.sse`，是脱敏后的真实形状：

```text
: keep-alive

data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"reasoning_content":"用户只是打招呼，"},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"reasoning_content":"简短回应即可。"},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"content":"好"},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"content":"，我是 DeepSeek。"},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":9,"total_tokens":21,"prompt_tokens_details":{"cached_tokens":0}}}

data: [DONE]
```

逐条读，可以得到几个重要观察：

- 第一个 chunk 只声明 `role`，`content` 是空字符串；
- `reasoning_content` 和 `content` 是两条独立通道，不能混在一起；
- `finish_reason` 出现在倒数第二个 JSON chunk；
- `usage` 单独一个 chunk，`choices` 为空；
- `[DONE]` 之后不应再有数据。

### 4.4 增量怎样拼成最终文本

```text
"你" + "好" + "，我是 DeepSeek。"  ->  "你好，我是 DeepSeek。"
```

代码上就是一个列表加 `"".join(...)`：

```python
text_parts.append(delta["content"])
final_text = "".join(text_parts)
```

不要用字符串 `+=` 在大循环里拼接，也不要假设每个 chunk 是完整句子。模型可能把 `"深圳"` 拆成 `"深"` 和 `"圳"`。

### 4.5 为什么必须有 `[DONE]`

不能靠“连接关闭”判断结束，因为：

- 连接可能因为网络问题提前断开；
- 有的网关会保持连接不关；
- 没有 `finish_reason` 就结束，意味着响应不完整，应该报错而不是提交半条消息。

---

## 5. 手写最小 `httpx` 客户端

在进入项目代码之前，先跑一遍这个文件：

```text
examples/21_minimal_http_sse.py
```

它只依赖 `httpx`，不导入 `pi_agent_py`，刻意只做四件事：

```text
构造请求 -> 发送请求 -> 打印状态码 -> 读取 SSE 并解析每个 JSON chunk
```

### 5.1 运行方式

```powershell
# 离线：使用脱敏 fixture，走完整 HTTP 客户端路径，但不访问网络
.\.venv\Scripts\python.exe .\python-pi-agent\examples\21_minimal_http_sse.py

# 非流式：对照一个完整 JSON 响应
.\.venv\Scripts\python.exe .\python-pi-agent\examples\21_minimal_http_sse.py --mode non-stream

# 工具参数分片
.\.venv\Scripts\python.exe .\python-pi-agent\examples\21_minimal_http_sse.py --fixture tool-call

# 真实调用（需要 API Key）
$env:DEEPSEEK_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe .\python-pi-agent\examples\21_minimal_http_sse.py --live --prompt "你好"
```

### 5.2 核心只有两个函数

构造请求：

```python
def build_payload(prompt: str, *, model: str, stream: bool) -> dict[str, object]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": stream,
        "thinking": {"type": "disabled"},
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload
```

拆 SSE：

```python
def iter_sse_data(lines):
    data_lines = []
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
```

### 5.3 输出长什么样

```text
传输方式: httpx.MockTransport + 脱敏 fixture（不访问网络）
========================================================================
请求方法: POST
请求 URL : https://api.deepseek.com/chat/completions
请求头   :
  authorization: Bearer sk-offl...-key
  content-type: application/json
  accept: text/event-stream
请求体   :
{ ... }
========================================================================
状态码      : 200
Content-Type: text/event-stream
逐条 SSE 事件:
  [01] 空 delta（例如角色声明或纯心跳）
  [02] reasoning_content='用户只是打招呼，'
  [03] reasoning_content='简短回应即可。'
  [04] content='你'
  [05] content='好'
  [06] content='，我是 DeepSeek。'
  [07] finish_reason='stop'
  [08] usage={'prompt_tokens': 12, 'completion_tokens': 9, 'total_tokens': 21, ...}
  [09] [DONE]  <- 流结束标记，不是 JSON
------------------------------------------------------------------------
拼接后的 thinking: '用户只是打招呼，简短回应即可。'
拼接后的 content : '你好，我是 DeepSeek。'
```

走到这里，你应该能回答：

- 哪些是 HTTP 知识？（方法、URL、Header、状态码、`Content-Type`）
- 哪些是 DeepSeek 协议？（`choices[].delta`、`finish_reason`、`usage`、`[DONE]`）
- 哪些是项目自己的抽象？（还没有出现——这正是下一节要引入的）

### 5.4 三个练习

1. 把 `--mode non-stream` 的响应体改成读取 `stream: false` 的真实返回，确认字段位置差异。
2. 在 fixture 中间插入一行 `: ping`，确认解析器不受影响。
3. 把某个 `data:` 改成非法 JSON，观察 `json.loads` 在哪里抛错——这正是 Provider 需要把错误分类的地方。

---

## 6. 为什么需要 Provider

### 6.1 如果 Runtime 直接解析 SSE

假设让 `Agent` 直接写 `httpx`：

```python
async for line in response.aiter_lines():
    if line.startswith("data:"):
        chunk = json.loads(line[5:])
        text = chunk["choices"][0]["delta"].get("content")
```

短期能跑，但供应商细节会渗进核心层：

- 换一家供应商，`choices[0].delta` 可能叫 `content_block_delta`；
- 加一个 Provider，就要在 Runtime 里加一堆 `if vendor == ...`；
- 测试 Agent Loop 时必须伪造 HTTP，而不是伪造领域事件；
- 工具调用分片、thinking、usage 的差异会污染消息聚合逻辑。

### 6.2 端口只认识统一事件

`ports.py` 里的 `ModelProvider` 只有一个方法：

```python
class ModelProvider(Protocol):
    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] = (),
        *,
        context: ProviderRequestContext,
    ) -> AsyncIterator[ModelStreamEvent]:
        ...
```

关键在返回类型 `ModelStreamEvent`。它在 `domain.py` 中定义为：

```python
ModelStreamEvent = TextDelta | ThinkingDelta | ToolCall | TokenUsage | Finish
```

五个事件各有明确职责：

| 事件 | 语义 | Runtime 的处理 |
|---|---|---|
| `ThinkingDelta(text)` | 一段思考文本 | 追加到 `Message.thinking`，发 live 事件 |
| `TextDelta(text)` | 一段可见文本 | 追加到 `Message.content`，发 live 事件 |
| `ToolCall(id, name, arguments)` | 一个**完整**工具调用 | 收集，交给工具系统 |
| `TokenUsage(...)` | 一次请求的 token 统计 | 记录到 `AgentResult.usage` |
| `Finish(reason)` | 本轮结束原因 | 决定提交与后续控制流 |

注意 `ToolCall` 是完整的，不是分片。分片是供应商协议的事，Provider 必须自己消化。

### 6.3 Provider 是适配器，不是“调用 API 的工具类”

这是全章最重要的概念：

> Provider 不是“封装了一个 HTTP 请求”的工具类，而是**供应商协议与 Agent 领域协议之间的适配器**。

它负责：

- 认证与 HTTP 传输；
- SSE 拆帧；
- 供应商字段到领域事件的映射；
- 供应商错误到领域错误的映射；
- 生命周期（连接池关闭）；
- 供应商特有的状态回传（例如 thinking 的 `reasoning_content`）。

它不负责：

- 决定何时调用模型（Runtime）；
- 执行工具（Tool 系统）；
- 保存会话（SessionStore）；
- 重试策略（当前版本明确未实现）；
- 面向用户的展示（EventSink / UI）。

### 6.4 能力闭环的五个部分

一个 Provider 能力要成立，五部分缺一不可：

1. **Definition**：`ModelProvider.stream()` 与统一事件；
2. **Provider**：`providers/deepseek.py`；
3. **Consumer**：`Agent.run()`；
4. **Lifecycle**：`async with DeepSeekProvider(...)` / `aclose()`；
5. **Evidence**：`tests/test_deepseek_provider.py` 的离线测试。

---

## 7. `DeepSeekProvider` 请求构造

### 7.1 配置对象

`DeepSeekConfig` 是一个冻结的 dataclass：

| 字段 | 默认值 | 作用 |
|---|---|---|
| `api_key` | 必填 | 认证；`repr=False`，不会出现在日志里 |
| `model` | `deepseek-flash` | 模型名 |
| `base_url` | `https://api.deepseek.com` | 便于代理或兼容网关 |
| `timeout_seconds` | `120.0` | 单次请求超时 |
| `thinking` | `disabled` | `enabled` / `disabled` |
| `reasoning_effort` | `None` | 思考强度；要求 thinking 开启 |
| `max_tokens` | `None` | 输出上限 |
| `user_id` | `None` | 供应商侧的用户标识 |

`__post_init__` 做了严格的启动期校验：

- API Key 和 model 不能为空白；
- `base_url` 必须是绝对的 HTTP(S) URL；
- timeout、max_tokens 必须为正；
- `reasoning_effort` 只能在 thinking 开启时设置；
- `user_id` 必须匹配 `[a-zA-Z0-9_-]{1,512}`。

这类校验的价值是**把配置错误提前到构造阶段**，而不是等第一次请求失败再猜。

### 7.2 两条生命周期原则

```python
self._client = client or httpx.AsyncClient()
self._owns_client = client is None
```

1. **API Key 由应用层传入，Provider 不偷偷读环境变量。** 这样 CLI、Web 服务、密钥管理器可以各自决定来源；测试也能显式传假 Key。
2. **谁创建 client，谁负责关闭。** Provider 自己创建的 client 在 `aclose()` 时关闭；外部注入的 client 不替调用者关闭。

`aclose()` 还是幂等的：重复调用不会报错。关闭之后再 `stream()`，会得到明确的 `ProviderError("DeepSeek provider is closed")`。

### 7.3 `_build_payload()` 的三段转换

```text
项目 Message        -> DeepSeek messages
项目 ToolDefinition -> DeepSeek tools/function schema
配置                -> model / stream / thinking / max_tokens
```

核心代码：

```python
payload = {
    "model": self._config.model,
    "messages": [
        _serialize_message(
            message,
            include_reasoning_content=include_reasoning_content,
        )
        for message in messages
    ],
    "stream": True,
    "stream_options": {"include_usage": True},
    "thinking": {"type": self._config.thinking},
}
```

工具 schema 的转换很薄，但边界必须清楚：

```python
{
    "type": "function",
    "function": {
        "name": definition.name,
        "description": definition.description,
        "parameters": dict(definition.input_schema),
    },
}
```

`ToolDefinition` 里的 `side_effects`、`requires_approval`、`retry_safe` 等安全元数据**不会发给模型**。它们是 Runtime 的执行策略，不是模型协议的一部分。这是“模型可见”与“执行安全”两条边界的典型例子。

### 7.4 thinking 模式下的 `reasoning_content` 回传

DeepSeek 的 thinking 模式在**携带工具的多轮请求**中，要求客户端把上一轮完整的 `reasoning_content` 原样传回：

```python
include_reasoning_content = (
    self._config.thinking == "enabled" and bool(tools)
)
```

链路是：

```text
reasoning_content SSE chunks
→ ThinkingDelta live events
→ committed Message.thinking
→ tool result
→ 下一次请求的 assistant.reasoning_content
```

为什么只在“thinking 开启且有工具”时回传？

- 没有工具时，通常不需要把推理内容再送回去；
- thinking 关闭时，字段没有意义；
- 盲目回传可能增加 token，甚至触发供应商校验失败。

`Message.thinking` 会随 assistant 消息进入 durable session log。JSONL schema v2 保存该字段，读取旧 schema v1 时使用空字符串，因此进程重启后的 replay 不会丢失新日志中的 reasoning 状态。日志可能包含模型推理内容，应与普通会话历史使用相同的访问控制和保留策略。

---

## 8. SSE 拆帧与 JSON 解析

### 8.1 `stream()` 的执行顺序

```text
_build_payload()
  → POST /chat/completions
  → 检查 status code
  → 检查 Content-Type
  → 逐行读取 SSE
  → 每个 data 字段交给 _normalize_chunk()
```

代码骨架：

```python
async with self._client.stream(
    "POST",
    url,
    headers=headers,
    json=payload,
    timeout=self._config.timeout_seconds,
) as response:
    if response.status_code != 200:
        raise await _http_error(response)
    content_type = response.headers.get("Content-Type", "")
    if "text/event-stream" not in content_type:
        raise ProviderResponseError(
            "DeepSeek streaming response is not text/event-stream"
        )

    tool_calls = _ToolCallAccumulator()
    async for data in _iter_sse_data(response):
        if data == "[DONE]":
            return
        for event in _normalize_chunk(data, tool_calls):
            yield event
```

为什么先检查 `status_code` 再检查 `Content-Type`？

- 401/429 的错误响应通常是 JSON，不是 SSE；
- 如果先按 SSE 解析，错误信息会被丢掉；
- 200 也可能是网关返回的 HTML 或普通 JSON，必须显式拒绝。

### 8.2 拆帧实现

```python
async def _iter_sse_data(response):
    data_lines = []
    async for line in response.aiter_lines():
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
```

四个细节都对应协议规则：

| 代码 | 对应规则 |
|---|---|
| `line == ""` | 空行结束一个事件 |
| `line.startswith(":")` | 注释 / keep-alive 丢弃 |
| `line[5:].lstrip()` | 去掉 `data:` 前缀和可选空格 |
| 结尾再 `yield` | 防止最后一个事件没有以空行结尾 |

不要用 `line.split("data:")` 或正则硬切。SSE 允许一个事件多行 `data:`，也允许字段顺序变化；按行状态机处理才是最稳的。

### 8.3 错误映射

`_http_error()` 把 HTTP 状态码翻译成领域错误：

| 状态码 | 异常 | 是否可重试 |
|---|---|---|
| 401 / 403 | `ProviderAuthenticationError` | 否 |
| 402 | `ProviderBalanceError` | 否 |
| 429 | `ProviderRateLimitError` | 当前不自动重试 |
| 其他 4xx | `ProviderHTTPError(retryable=False)` | 否 |
| 5xx | `ProviderHTTPError(retryable=True)` | 当前不自动重试 |

错误消息会从响应体中提取 `error.message`，并截断到 500 字符。它绝不会包含 API Key；测试里专门断言了 `repr(config)` 和异常文本都不含密钥。

网络层异常也在 `stream()` 里统一映射：

```python
except ProviderError:
    raise
except httpx.TimeoutException as error:
    raise ProviderTimeoutError(
        f"DeepSeek request timed out after {self._config.timeout_seconds:g}s"
    ) from error
except httpx.HTTPError as error:
    raise ProviderError(f"DeepSeek transport failed: {type(error).__name__}") from error
```

三个顺序很重要：

1. `ProviderError` 直接重抛，避免自己包装自己；
2. 超时要在通用 `HTTPError` 之前捕获，因为 `TimeoutException` 是它的子类；
3. `asyncio.CancelledError` 不在捕获列表里，必须原样向上传播——取消不是失败，不能被包装成 `ProviderError`。

---

## 9. 文本、thinking、usage 的标准化

### 9.1 一张映射表

```text
delta.reasoning_content -> ThinkingDelta
delta.content           -> TextDelta
delta.tool_calls        -> ToolCall（累积后）
usage                    -> TokenUsage
finish_reason           -> Finish
```

`_normalize_chunk()` 按顺序处理：

```python
reasoning = delta.get("reasoning_content")
if isinstance(reasoning, str) and reasoning:
    yield ThinkingDelta(reasoning)

content = delta.get("content")
if isinstance(content, str) and content:
    yield TextDelta(content)
```

为什么要判断 `and reasoning` / `and content`？

- 第一个 chunk 的 `content` 是空字符串，只用来声明角色；
- 空 delta 不应该产生空事件，否则 Runtime 会收到无意义的 `TextDelta("")`；
- 当前实现只接受字符串，非字符串字段会被忽略；如果要把“类型不对”当成协议错误，需要在结构校验层显式加断言，而不是靠 `isinstance` 静默跳过。

### 9.2 `finish_reason` 的当前支持范围

项目当前只映射三种：

| `finish_reason` | 领域事件 |
|---|---|
| `stop` | `Finish("stop")` |
| `length` | `Finish("length")` |
| `tool_calls` | `Finish("tool_calls")` |

官方文档还列出了 `content_filter`、`insufficient_system_resource`、`aborted` 等结束原因，但当前 `FinishReason` 类型只覆盖上面三种。遇到未映射的值，Provider 会显式抛错：

```python
raise ProviderResponseError(
    f"DeepSeek finish reason is not supported yet: {finish_reason}"
)
```

这是有意的 fail-fast：**宁可明确失败，也不要把内容审核中断悄悄当成正常结束。** 后续如果要支持更多原因，应该同时扩展 `FinishReason`、Runtime 的提交语义和 UI 的展示，而不是只在 Provider 里加一行 `else: Finish("stop")`。

### 9.3 `usage` 的位置与归一化

`usage` 不一定和 `choices` 在同一个 chunk。最后一个 chunk 可能长这样：

```json
{"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":9,"total_tokens":21}}
```

所以 `_normalize_chunk()` 在 `choices` 处理完之后，独立检查 `usage`：

```python
usage = chunk.get("usage")
if isinstance(usage, dict):
    yield _normalize_usage(usage)
```

`_normalize_usage()` 要求三个字段都是整数，并可选读取缓存命中：

```python
TokenUsage(
    input_tokens=prompt_tokens,
    output_tokens=completion_tokens,
    total_tokens=total_tokens,
    cached_input_tokens=cached_tokens,  # prompt_tokens_details.cached_tokens
)
```

为什么不直接透传供应商的 `usage` 字典？

- Runtime 不应该知道 `prompt_tokens` 这个名字；
- 不同供应商的字段名不同（`input_tokens` / `prompt_tokens` / `inputTokens`）；
- 归一化后才能做统一的成本统计和预算控制。

### 9.4 流内错误

有些错误不在 HTTP 状态码里，而是在 SSE 数据里：

```json
{"error":{"message":"upstream failed"}}
```

`_normalize_chunk()` 会在解析 `choices` 之前检查 `error` 字段，并抛出 `ProviderResponseError`。否则这个 chunk 会被当成“没有 delta 的空事件”静默跳过，最终以“没有 finish”收场，错误信息全部丢失。

---

## 10. 工具调用为什么需要分片累积

### 10.1 真实分片长什么样

模型决定调用 `get_weather({"city": "深圳"})`，SSE 里可能是：

```text
data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"weather","arguments":"{\"city\":"}}]},"finish_reason":null}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"深圳\"}"}}]},"finish_reason":null}]}

data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}
```

第一个分片有 `id`、`name` 和半个参数；第二个分片只有剩下的参数；第三个分片才告诉你“工具调用结束了”。

### 10.2 错误做法

```python
# 错误：把每个 chunk 当成完整工具调用
for raw in delta["tool_calls"]:
    call = ToolCall(
        id=raw["id"],                    # 第二个分片没有 id
        name=raw["function"]["name"],    # 第二个分片没有 name
        arguments=json.loads(raw["function"]["arguments"]),  # 半个 JSON
    )
```

这会立刻炸在三个地方：缺 `id`、缺 `name`、`json.loads` 失败。

### 10.3 正确做法：按 `index` 累积

`_ToolCallAccumulator` 用 `index` 作为主键：

```python
parts = self._calls.setdefault(index, _ToolCallParts())
```

每个分片只更新“自己有”的字段：

- `id`：只在出现时设置；如果后续分片给了不同的 `id`，直接报错；
- `name`：同上，防止流中途换工具；
- `arguments`：**追加到列表**，不是覆盖。

累积完成后统一构建：

```python
raw_arguments = "".join(parts.argument_chunks) or "{}"
arguments = json.loads(raw_arguments)
if not isinstance(arguments, dict):
    raise ProviderResponseError("DeepSeek tool arguments must decode to an object")
```

四个校验都不能省：

| 校验 | 防止的问题 |
|---|---|
| `id` 和 `name` 必须存在 | 半截工具调用被误执行 |
| 同名/同 id 不能被改写 | 流被拼接或串包 |
| 参数必须是字符串 | 供应商返回对象而不是文本 |
| 解析结果必须是 object | 模型返回数组或标量，工具系统无法按 schema 校验 |

### 10.4 为什么在 `finish_reason` 时才 `build()`

`_normalize_chunk()` 在收到 `finish_reason` 时做两件事：

```python
if finish_reason is not None:
    if tool_calls.has_calls:
        yield from tool_calls.build()
    ...
```

顺序不能反：

1. 先 `build()`，保证所有参数分片都已到齐；
2. 再 `Finish("tool_calls")`，告诉 Runtime“这一轮结束了”。

如果先发 `Finish`，Runtime 可能在工具调用还没构建完时就结束收集，导致工具丢失。

### 10.5 一个容易忽略的协议约束

Runtime 在 `_request_model()` 里会检查：

```python
if tool_calls and finish.reason not in {"tool_calls", "length"}:
    raise ModelProtocolError(...)
if not tool_calls and finish.reason == "tool_calls":
    raise ModelProtocolError(...)
```

这不是 Provider 的责任，而是**跨组件的协议契约**：Provider 说“有工具调用”，就必须同时给出 `tool_calls` 结束原因；说“工具调用结束”，就必须真的产出了 `ToolCall`。这类检查放在 Runtime，才能覆盖所有 Provider，而不是在每个适配器里各写一遍。

---

## 11. Runtime 如何消费统一事件

`runtime/loop.py::Agent._request_model()` 是 Agent 循环的模型流消费点。它维护这些局部变量：

```python
partial_text: list[str] = []
partial_thinking: list[str] = []
tool_calls: list[ToolCall] = []
usage: TokenUsage | None = None
finish: Finish | None = None
```

然后逐个事件聚合：

| 事件 | Runtime 动作 | 发出的事件 |
|---|---|---|
| `ThinkingDelta` | `partial_thinking.append(...)` | `thinking_delta`（live） |
| `TextDelta` | `partial_text.append(...)` | `text_delta`（live） |
| `ToolCall` | 去重后加入 `tool_calls` | `tool_call_received`（capability） |
| `TokenUsage` | 覆盖 `usage` | `model_usage`（live） |
| `Finish` | 记录 `finish` | 无（由后续流程决定） |

聚合完成后构造 assistant 消息：

```python
Message(
    role="assistant",
    content="".join(partial_text),
    thinking="".join(partial_thinking),
    tool_calls=tuple(tool_calls),
)
```

这一步完成了从“原始字节”到“领域消息”的闭环：

```text
SSE bytes
  -> JSON chunk
  -> ModelStreamEvent
  -> partial_text / partial_thinking / tool_calls
  -> Message
  -> durable session log
```

### 11.1 Runtime 做的四项协议校验

1. **`Finish` 之前不能再有内容事件。** `_require_unfinished()` 会拒绝 `Finish` 之后的 `TextDelta` / `ThinkingDelta` / `ToolCall`。
2. **只能有一个 `Finish`。** 重复结束是协议违规。
3. **流必须显式结束。** 没有 `Finish` 就结束，抛 `ModelProtocolError`。
4. **工具调用与结束原因必须一致。** 见上一节。

这些校验的意义是：Provider 可以有 bug，但 bug 不能静默变成一条“看起来正常”的消息。

### 11.2 live 事件与 durable 事件的区别

`ThinkingDelta` 和 `TextDelta` 都只发 **live** 事件，用于 UI 实时显示。真正进入会话历史的是聚合后的 `Message`，通过 `message_committed` 这个 **durable** 事件提交。

这解释了一个常见疑问：

> 为什么流式输出已经显示了，进程崩溃后却没有这条消息？

因为 live 事件不是状态，durable 提交才是。UI 上的文字只是“正在生成”，不是“已提交”。

---

## 12. 使用 `MockTransport` 验证协议

`tests/test_deepseek_provider.py` 是一份**可执行的协议文档**。它不访问网络，也不消耗 API 额度。

### 12.1 覆盖范围

| 测试 | 证明什么 |
|---|---|
| `test_normalizes_stream_and_integrates_with_agent` | 文本、thinking、usage、Header、请求体、Agent 集成 |
| `test_maps_authentication_error_without_exposing_api_key` | 401 映射，且不泄漏密钥 |
| `test_normalizes_split_tool_call_and_sends_tool_schema` | 工具参数分片累积、schema 发送 |
| `test_replays_reasoning_content_after_thinking_tool_call` | 两轮请求中 reasoning 回传 |
| `test_rejects_malformed_stream_json` | 非法 JSON |
| `test_rejects_non_sse_success_response` | 200 但不是 SSE |
| `test_maps_rate_limit_error` | 429 映射 |
| `test_maps_timeout_to_provider_timeout_error` | 超时映射为 `ProviderTimeoutError` |
| `test_task_cancellation_propagates_without_wrapping` | 取消原样传播 |
| `test_rejects_unsupported_finish_reason_explicitly` | 未映射结束原因显式失败 |
| `test_owned_client_close_is_idempotent` | 自建 client 幂等关闭 |
| `test_external_client_is_not_closed_by_provider` | 外部 client 不被关闭 |
| `test_validates_configuration_without_revealing_secret` | 配置校验与密钥隐藏 |

### 12.2 一个测试的完整结构

```python
def handle(request: httpx.Request) -> httpx.Response:
    body = "\n".join([
        ": keep-alive",
        "",
        'data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}',
        "",
        'data: {"choices":[{"delta":{"content":"好"},"finish_reason":null}]}',
        "",
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
        "",
    ])
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        content=body.encode(),
    )

client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
provider = DeepSeekProvider(DeepSeekConfig(api_key="test-key"), client=client)
events = await _collect(provider, [Message(role="user", content="hello")])
```

模式始终是：

```text
伪造原始 SSE
  -> 调用 Provider
  -> 检查标准事件
  -> 不访问网络、不消耗 API 额度
```

### 12.3 超时与取消为什么必须单独测

超时和取消是最容易被“顺手包装错”的两条路径：

- 超时如果被包成普通 `ProviderError`，上层就无法区分“网络慢”和“协议错”；
- 取消如果被 `except Exception` 吞掉，`asyncio` 的取消语义会被破坏，任务无法及时退出。

测试用 `httpx.ReadTimeout` 和“读到一半阻塞的 `AsyncByteStream`”分别验证这两条路径。它们比正常路径更值得写测试，因为正常路径出问题时你能看见，取消路径出问题时往往只是“偶尔卡住”。

### 12.4 运行

```powershell
.\.venv\Scripts\python.exe -m unittest python-pi-agent.tests.test_deepseek_provider -v
```

预期 13 个测试全部通过。

### 12.5 fixture 也是文档

`examples/fixtures/` 下保存了脱敏后的三种响应：

- `deepseek_stream_text.sse`：文本 + thinking + usage；
- `deepseek_stream_tool_call.sse`：工具参数分片；
- `deepseek_non_stream.json`：非流式完整响应。

它们同时被最小客户端和教学文档使用。新增 Provider 时，建议先保存 fixture，再写解析代码——这能避免“对着真实 API 反复试”的低效循环。

---

## 13. 在完整 Agent 中运行

`examples/02_deepseek_chat.py` 展示了完整应用链路：

```text
环境变量读取 Key
  → DeepSeekConfig
  → DeepSeekProvider
  → Agent
  → EventSink 打印流式文本
  → RunResult 显示 token usage
```

核心代码：

```python
config = DeepSeekConfig(
    api_key=require_api_key(),
    model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
    thinking=thinking,
)

async with DeepSeekProvider(config) as provider:
    agent = Agent(
        AgentComponents(
            provider,
            system_prompt="你是一个简洁、可靠的中文助手。",
        ),
        event_sink=CallbackEventSink(observe),
    )
    result = await agent.run(prompt)
```

`observe` 只关心 `text_delta`：

```python
def observe(event: AgentEvent) -> None:
    if event.kind == "text_delta":
        print(event.payload["text"], end="", flush=True)
```

运行：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
$env:DEEPSEEK_MODEL = "deepseek-flash"
.\.venv\Scripts\python.exe .\python-pi-agent\examples\02_deepseek_chat.py
```

思考模式可选：

```powershell
$env:DEEPSEEK_THINKING = "enabled"
```

这一节不要重复解释 SSE。重点回答一个问题：

> 前面写好的 Provider，怎样被 Agent 当成普通可替换组件使用？

答案是：`Agent` 的构造函数只要求一个 `ModelProvider` 协议对象。它不知道 `DeepSeekProvider` 的存在，也不知道 `httpx`、SSE、`reasoning_content`。切换 Provider 时，`Agent` 一行都不用改。

---

## 14. 对照开源 Pi 的生产级拆分

完整开源案例使用仓库里固定版本的 `pi/` submodule，当前指向：

```text
56b25ff4ebbd8a119ef9185447c7b2416059dbcb
```

建议按调用链阅读，而不是按目录顺序读。

### 14.1 四层职责

1. `pi/packages/ai/src/providers/deepseek.ts`

   只有 15 行，定义 DeepSeek 的**身份**：

   ```ts
   export function deepseekProvider(): Provider<"openai-completions"> {
       return createProvider({
           id: "deepseek",
           name: "DeepSeek",
           baseUrl: "https://api.deepseek.com",
           auth: { apiKey: envApiKeyAuth("DeepSeek API key", ["DEEPSEEK_API_KEY"]) },
           models: Object.values(DEEPSEEK_MODELS),
           api: openAICompletionsApi(),
       });
   }
   ```

   它不解析 SSE，也不构造消息。它只回答“我是谁、用哪个 API 实现、支持哪些模型”。

2. `pi/packages/ai/src/api/openai-completions.ts`

   共享 OpenAI-compatible 协议的**实现**：请求构造、流式解析、reasoning、工具调用、usage、重试、缓存。DeepSeek 只是它的一个配置实例。

3. `pi/packages/ai/src/models.ts`

   根据 model 找到 provider，再找到对应的 stream 实现，并在这里完成认证、base URL 覆盖和请求选项合并。

4. `pi/packages/agent/src/agent-loop.ts`

   消费统一的 `text_delta`、`thinking_delta`、`toolcall_delta` 和完成事件，把 partial message 替换成最终消息。它同样不认识 DeepSeek。

### 14.2 对照表

| 本项目 | Pi |
|---|---|
| DeepSeek HTTP、SSE、转换暂时集中在一个 Python 文件 | DeepSeek 配置与 OpenAI-compatible 协议实现分离 |
| 便于初学者看到完整机制 | 便于大量 Provider 共享能力 |
| 先建立一个清晰纵向切片 | 在多个 Provider 出现后提炼通用层 |
| 一个 `DeepSeekProvider` 类 | `Provider` 描述 + `api` 实现 + `Models` 调度 |
| `Finish` / `TokenUsage` 是项目领域事件 | `AssistantMessageEvent` 是统一事件流 |
| 无自动重试 | 内置 `retryProviderRequest` 与 `maxRetries` |
| 保存 reasoning 文本 | 还保存 provider-specific thinking signature |

### 14.3 为什么不要先读 Pi

Pi 的抽象已经比较成熟。如果读者没有亲手解析过一次 SSE，很容易只记住“注册一个 provider 配置”，却不知道底层发生了什么：

- 为什么工具参数要按 index 累积？
- 为什么 `usage` 可能单独一个 chunk？
- 为什么 `[DONE]` 不是 JSON？
- 为什么取消不能被包装？

这些问题的答案在字节层，不在抽象层。**先纵向走通一遍，再横向看抽象**，是这一章的教学顺序。

---

## 15. 独立实现第二个 Provider

### 15.1 第一轮：另一个 OpenAI-compatible Provider

目标是判断哪些代码可以复用，哪些配置必须独立。

完成顺序：

1. 阅读供应商官方请求和响应 schema。
2. 保存脱敏后的非流式和流式 fixture。
3. 定义配置对象。
4. 实现消息和工具序列化。
5. 解析文本、usage、finish reason。
6. 实现工具调用参数累积。
7. 映射认证、限流、超时和服务端错误。
8. 使用 `MockTransport` 编写离线测试。
9. 接到现有 `Agent`，不修改 Agent Loop。

验收标准：

```text
同一个 Agent
  + 两个 Provider
  + 同一组测试断言
  = 只换构造函数参数，Runtime 零改动
```

第一家完成后**先不要急着抽象公共基类**。等第二个 Provider 写完，再根据真实重复代码决定是否提取 OpenAI-compatible adapter。

### 15.2 第二轮：协议不同的 Provider

再选择一个不是 Chat Completions 结构的 API（例如 Anthropic Messages、Gemini、Bedrock Converse）。重点练习：

- 请求消息格式不同；
- 流式事件名称不同；
- 工具调用表示不同；
- usage 出现位置不同；
- 完成事件和错误语义不同。

最终验证目标：

```text
DeepSeekProvider ─┐
Provider B       ─┼→ ModelStreamEvent → 同一个 Agent Runtime
Provider C       ─┘
```

如果切换 Provider 需要修改 Runtime，就说明供应商细节泄漏进了核心层。

### 15.3 泄漏检查清单

实现完成后，用这份清单自查：

- [ ] 已确认 `runtime/loop.py` 只依赖规范化模型事件；
- [ ] `domain.py` 没有出现 `reasoning_content`、`choices`、`prompt_tokens`；
- [ ] 新 Provider 的测试不需要真实 API Key；
- [ ] 错误类型是项目定义的 `ProviderError` 子类；
- [ ] 工具调用在 Provider 内部已经聚合成完整 `ToolCall`；
- [ ] 取消能原样传播；
- [ ] 关闭语义明确：谁创建，谁关闭。

任何一条不满足，都说明抽象边界还需要调整。

---

## 16. 两个实现完成后再提取公共兼容层

### 16.1 什么时候可以抽象

满足以下条件再动手：

1. 至少两个 Provider 已经能独立工作；
2. 重复代码是**结构性的**，不是偶然相似的几行；
3. 已有测试能覆盖提取前后的行为；
4. 你能说清“哪些差异是配置，哪些差异是协议”。

### 16.2 可以提取什么

- HTTP client 创建与关闭；
- 认证 Header 的构造；
- 通用错误映射（401/402/429/5xx）；
- SSE 拆帧；
- `usage` 的通用归一化；
- 工具参数累积器；
- 重试与退避策略。

### 16.3 不应该提取什么

- 供应商特有的 `thinking` 字段语义；
- 供应商特有的结束原因；
- 消息内容的多模态结构；
- 供应商特有的缓存 / signature 回传规则；
- 各家的工具调用表示。

这些差异应该留在具体 Provider 或兼容层配置里，而不是塞进一个“万能基类”。

### 16.4 三种可选结构

```text
方案 A：每个 Provider 独立实现
  DeepSeekProvider
  ProviderB

方案 B：共享 HTTP/SSE 工具函数
  DeepSeekProvider ─┐
  ProviderB        ─┼→ sse.py / errors.py / usage.py
                    ┘

方案 C：Provider 描述 + 协议实现分离（Pi 的做法）
  DeepSeek 配置 ─┐
  ProviderB 配置 ─┼→ OpenAICompatibleAPI
                  ┘
```

从 A 到 B 再到 C，抽象程度递增，理解成本也递增。教学项目应该按这个顺序演进，而不是一开始就做 C。

### 16.5 一个反模式

```python
class BaseProvider:
    def stream(self, messages, tools=()):
        if self.vendor == "deepseek":
            ...
        elif self.vendor == "openai":
            ...
```

这不是抽象，只是把 `if/else` 从 Runtime 搬到了基类。真正的抽象应该让新增 Provider **只写新文件**，而不是修改旧文件。

---

## 附录 A：16 节与六阶段的对应关系

| 阶段 | 对应小节 | 产物 |
|---|---|---|
| 第一阶段：看懂一次 API 调用 | 1–5 | `examples/21_minimal_http_sse.py` + fixtures |
| 第二阶段：理解 Provider | 6–11 | `providers/deepseek.py` 逐段阅读 |
| 第三阶段：用测试证明 | 12 | `tests/test_deepseek_provider.py` |
| 第四阶段：运行真实示例 | 13 | `examples/02_deepseek_chat.py` |
| 第五阶段：对照开源 Pi | 14 | `pi/` submodule 调用链 |
| 第六阶段：独立实现 Provider | 15–16 | 第二个 Provider + 抽象决策 |

## 附录 B：当前边界

- 已支持 DeepSeek 流式 tool call 聚合、参数校验、工具结果回填，以及 thinking 模式下的 reasoning 回传。
- 尚未自动重试 429/5xx；错误已分类，重试策略将在独立能力中实现。
- `Finish("length")` 会提交已生成文本，同时把原因返回给调用方。
- `finish_reason` 当前只支持 `stop` / `length` / `tool_calls`；其他值显式失败，不做静默降级。
- Provider 使用 Chat Completions；未来可以新增 Responses API Provider，而不修改 Agent Loop。
- 当前保存 reasoning 文本，但尚未实现 Pi 的 provider-specific thinking signature 与跨模型转换规则。

## 掌握标准

学完这一章，标准不是“能调通一次 API”，而是：

> 拿到一家新供应商的协议文档，在不改 Agent Loop 的前提下，实现、测试并接入一个新的 Provider。

如果你能独立完成这件事，并且能说清每一步为什么放在那个位置，这一章才算真正学完。
