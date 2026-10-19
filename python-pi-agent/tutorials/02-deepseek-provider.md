# 第二章：实现可替换的 DeepSeekProvider

## 本章目标

把真实 DeepSeek API 接到第一章的 `ModelProvider` 端口，同时保证 Agent Loop 不依赖供应商 SDK、HTTP 格式或 API Key 来源。

截至 2026-10-19，DeepSeek 官方 Chat Completions 接口使用：

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
usage                    -> TokenUsage
finish_reason=stop       -> Finish("stop")
finish_reason=length     -> Finish("length")
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

思考增量只作为 live event 观察，不写入 assistant 正文，也不进入下一轮模型上下文。

## 当前边界

- 尚未支持 DeepSeek tool call；收到 tool call 会明确失败，不会静默丢失。
- 尚未自动重试 429/5xx；错误已分类，重试策略将在独立能力中实现。
- `Finish("length")` 会提交已生成文本，同时把原因返回给调用方。
- Provider 使用 Chat Completions；未来可以新增 Responses API Provider，而不修改 Agent Loop。
