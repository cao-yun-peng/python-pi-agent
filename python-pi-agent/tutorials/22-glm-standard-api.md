# GLM 标准 API 适配

这一节把 GLM 接到现有 `ModelProvider` 端口。目标不是复制一套 OpenAI 客户端，而是验证哪些行为真的兼容、哪些仍属于 GLM 自己的协议策略。

## 位置与数据流

```text
Message / ToolDefinition
        ↓
GLMProvider：认证、模型能力、request_id、错误码
        ↓
_openai_compat：消息/工具序列化、SSE、工具分片、usage
        ↓
ThinkingDelta / TextDelta / ToolCall / TokenUsage / Finish
```

`GLMProvider` 使用标准 Chat Completions 地址：

```text
POST https://open.bigmodel.cn/api/paas/v4/chat/completions
Authorization: Bearer <ZAI_API_KEY>
Accept: text/event-stream
```

默认模型是 `glm-5.3`。实现会发送来自 `ProviderRequestContext` 的 `request_id`，并要求长度为 6–64；配置了工具且模型支持时发送 `tool_stream: true`。流只有在同时收到 `finish_reason` 与 `[DONE]` 后才成功结束，usage 会在 `Finish` 之前归一化交给运行时。

## 配置与运行

顶层 `agent.yaml`：

```yaml
version: 1
provider:
  name: glm
  api_key_env: ZAI_API_KEY
  model: glm-5.3
  base_url: https://open.bigmodel.cn/api/paas/v4
  thinking: enabled
  reasoning_effort: high
  max_tokens: 16000
```

然后使用已有的显式配置入口：

```powershell
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
python .\python-pi-agent\examples\22_provider_configuration.py set-key
python .\python-pi-agent\examples\22_provider_configuration.py check
python .\python-pi-agent\examples\22_provider_configuration.py chat
```

前两个命令不访问模型；只有 `chat` 会发起真实请求并可能产生费用。诊断只显示类似 `********5678` 的脱敏值。

## 模型能力为何留在适配器

- GLM-5.3/5.3-Flash 不能关闭 thinking；5.3 的 `reasoning_effort` 只接受 `low`、`high`、`max`。
- GLM-5.2 接受更完整的 effort 集合。
- 较早模型不应收到它们不支持的 `reasoning_effort`。
- `max_tokens` 在当前标准 API 范围内限制为 1–131072。

这些约束会随 GLM 模型族变化，放进共享 codec 会迫使 DeepSeek/Kimi 理解 GLM 型号，因此由 `GLMConfig` 在网络请求前失败关闭。

## 工具调用与 reasoning 重放

标准流里的 `delta.tool_calls` 可以把函数名和 JSON 参数拆成多个片段。共享 `ToolCallAccumulator` 按 index 聚合，并在 `finish_reason=tool_calls` 时生成完整领域 `ToolCall`。

本实现只为“最后一个 user 消息之后、带 tool call 的 assistant 消息”重放 `reasoning_content`。这样能维持当前工具续轮，又不会把更早轮次的思考全文无边界地重新发送。这个边界由离线回归测试锁定；如果官方后续要求签名或不同重放策略，应在 GLM 适配器内演进。

## 错误与取消

HTTP 401/403 映射认证错误；429 中官方业务码 `1113` 映射余额不足，其余 429 映射限流；5xx 映射可重试的 `ProviderHTTPError`。错误正文最多保留有界消息，不拼入 API key。HTTP timeout 转成 `ProviderTimeoutError`，任务取消保持原始 `CancelledError`，外部注入的客户端不会由 Provider 关闭。

测试位于 `tests/test_glm_provider.py`，通过 `httpx.MockTransport` 覆盖文本、思考、usage、工具分片、错误、超时、取消和关闭生命周期，不依赖真实密钥或付费 API。

掌握标准：能指出共享 wire codec 与 GLM policy 的边界，解释为什么 `request_id`、错误业务码和模型能力不能被塞进通用解析器，并能用一个新的 MockTransport fixture 复现协议变化。
