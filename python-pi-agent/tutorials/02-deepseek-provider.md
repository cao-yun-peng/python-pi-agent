# 第二章：实现可替换的 DeepSeekProvider

## 本章目标

把真实 DeepSeek API 接到第一章的 `ModelProvider` 端口，同时保证 Agent Loop 不依赖供应商 SDK、HTTP 格式或 API Key 来源。

截至 2026-10-20，DeepSeek 官方 Chat Completions 接口使用：

- Base URL：`https://api.deepseek.com`
- Endpoint：`POST /chat/completions`
- 推荐模型：`deepseek-flash`
- 流式格式：SSE `data:` 事件，以 `data: [DONE]` 结束
- 思考内容：`delta.reasoning_content`

官方文档：

- <https://api-docs.deepseek.com/api/create-chat-completion/>
- <https://api-docs.deepseek.com/quick_start/error_codes/>
- <https://api-docs.deepseek.com/quick_start/rate_limit/>

## Capability 的五个部分

1. **Definition**：`ModelProvider.stream()` 和通用模型事件。
2. **Provider**：`providers/deepseek.py`。
3. **Consumer**：`Agent.run()`。
4. **Lifecycle**：`async with DeepSeekProvider(...)` / `aclose()`。
5. **Evidence**：`tests/test_deepseek_provider.py` 的 MockTransport 测试。

## 协议转换

DeepSeek SSE 不应直接进入 Agent Loop。Provider 将供应商字段转换成：

```text
delta.reasoning_content -> ThinkingDelta
delta.content           -> TextDelta
delta.tool_calls        -> ToolCall
usage                    -> TokenUsage
finish_reason=stop       -> Finish("stop")
finish_reason=length     -> Finish("length")
finish_reason=tool_calls -> Finish("tool_calls")
```

keep-alive 注释和空行属于传输层细节，由 Provider 消化。

## API Key 边界

`DeepSeekConfig` 要求显式传入 API Key，但不会在 `repr` 中显示。Provider 自身不读取环境变量；示例应用负责读取 `DEEPSEEK_API_KEY`。这样 Web 服务、密钥管理器和测试可以采用不同的配置来源。

## 运行

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
$env:DEEPSEEK_MODEL = "deepseek-flash"
.\.venv\Scripts\python.exe .\python-pi-agent\examples\02_deepseek_chat.py
```

思考模式是可选的：

```powershell
$env:DEEPSEEK_THINKING = "enabled"
```

思考增量会立即作为 live event 发出，但只有收到 `Finish` 后，拼接完成的内容才写入
assistant message 的 `thinking` 字段。它不混入面向用户的 `content`，却属于供应商继续
对话所需的模型可见状态。

## Thinking ToolCall 的回传

DeepSeek 的 thinking 模式在携带工具的多轮请求中要求客户端把上一轮完整
`reasoning_content` 原样传回。链路如下：

```text
reasoning_content SSE chunks
→ ThinkingDelta live events
→ committed Message.thinking
→ tool result
→ next DeepSeek request assistant.reasoning_content
```

`Message.thinking` 会和 assistant message 一起进入 durable session log。JSONL schema v2
保存该字段，读取旧 schema v1 时使用空字符串，因此进程重启后的 replay 不会丢失新日志
中的 reasoning 状态。日志可能包含模型推理内容，应与普通会话历史使用相同的访问控制和
保留策略。

`tests/test_deepseek_provider.py` 使用两轮 `MockTransport`：第一轮流式返回 reasoning 和
ToolCall，工具执行后检查第二次请求体确实包含原始 `reasoning_content`。测试不访问网络，
也不产生 API 费用。

## 当前边界

- 已支持 DeepSeek 流式 tool call 聚合、参数校验、工具结果回填，以及 thinking 模式下的 reasoning 回传。
- 尚未自动重试 429/5xx；错误已分类，重试策略将在独立能力中实现。
- `Finish("length")` 会提交已生成文本，同时把原因返回给调用方。
- Provider 使用 Chat Completions；未来可以新增 Responses API Provider，而不修改 Agent Loop。
- 当前保存 reasoning 文本，但尚未实现 Pi 的 provider-specific thinking signature 与跨模型转换规则。
