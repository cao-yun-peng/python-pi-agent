# TiMi Codex/GPT/Grok Responses API 适配

这一节解决一个明确问题：在不修改 Agent loop、也不把 Responses API 硬塞进 Chat Completions codec 的前提下，让同一个 `ModelProvider` 端口驱动 TiMi CC 上的 Codex、GPT 和 Grok 模型。

## 协议边界

TiMi 的 Codex 文档使用 `wire_api = "responses"`，实际端点为 `https://timicc.com/v1/responses`；Grok CLI 文档同样指定 `api_backend = "responses"`。模型可用性由 API key 分组决定，所以配置层没有猜测默认模型。

Responses 与 Chat Completions 只共享 HTTP/SSE framing：

- 流事件由 `response.output_text.delta`、`response.output_item.done`、`response.completed` 等 `type` 区分，不使用 `choices[].delta` 与 `[DONE]`。
- function tool 是扁平的 `type/name/description/parameters/strict`，function result 使用 `function_call_output + call_id`。
- usage 使用 `input_tokens/output_tokens/total_tokens`。
- reasoning 模型在 `store: false` 下返回带 `encrypted_content` 的 reasoning item，并要求把 output item 原样放入下一次 `input`。当前 OpenAI API 默认返回该字段；适配器仍显式发送向后兼容的 `include: ["reasoning.encrypted_content"]`，便于对接实现较早版本协议的中转层。

因此本项目只把 framing 提到 `_sse.py`。`_openai_compat.py` 继续专属于 Chat Completions；`timicc_responses.py` 独立拥有 typed event、output item、工具和完成语义，没有 `BaseOpenAIProvider`。

## 可观察执行轨迹

```text
user Message
  -> POST /v1/responses (stream=true, store=false)
  -> reasoning/text delta       -> live observer event
  -> output_item.done           -> ProviderState + optional ToolCall
  -> response.completed         -> TokenUsage + Finish
  -> complete assistant Message -> durable commit
```

`ProviderState` 是 provider-neutral 的承载容器，但其中 JSON 由具体 Provider 拥有。runtime 只深拷贝和持久化，其他适配器忽略它，普通 `message_committed` observer payload 不包含它。这样既能在 JSONL v6 重启后续接 reasoning/tool loop，也不会让 encrypted reasoning content 意外进入应用日志。

## 配置与运行

```yaml
version: 1
provider:
  name: timicc
  api_key_env: TIMI_CC_API_KEY
  model: gpt-6-astra  # 替换为 key 分组实际授权的模型
  base_url: https://timicc.com/v1
  reasoning_effort: high
  reasoning_summary: auto
  max_output_tokens: 16000
```

```powershell
Copy-Item .\agent.yaml.example .\agent.yaml
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
python .\python-pi-agent\examples\22_provider_configuration.py set-key
python .\python-pi-agent\examples\22_provider_configuration.py check
python .\python-pi-agent\examples\22_provider_configuration.py chat
```

前两条子命令不访问模型；`chat` 会发起真实请求并可能产生费用。

## 失败、取消与证据

适配器在网络前校验 key、显式 model、URL、timeout、reasoning 枚举和最小输出 token。HTTP 401/403、402、429、5xx 进入稳定领域错误；非 SSE、畸形 JSON、未知终止、函数参数不一致或流没有 terminal event 都 fail closed。超时转换为 `ProviderTimeoutError`，取消原样传播，外部注入的 `httpx.AsyncClient` 不由 Provider 关闭。

`tests/test_timicc_responses_provider.py` 使用 `httpx.MockTransport` 离线覆盖文本、reasoning、usage、function argument 分片、opaque item 回放、HTTP 错误、incomplete 与 timeout；`test_minimal_loop.py` 证明 observer 不泄漏 opaque state；`test_jsonl_session_store.py` 证明 v6 往返、非当前 schema 拒绝和必需字段校验。没有测试调用付费 API。

## 主动省略

当前固定 `store: false`，不使用 `previous_response_id` 或服务端 conversation；没有自动模型发现、router、retry/backoff，也没有把 provider state 转换给另一家模型。这些都需要独立策略与失败语义，不应悄悄进入 wire adapter。

官方对照：

- [TiMi Codex 配置](https://docs.timicc.com/docs/faq/Codex.html)
- [TiMi Grok CLI 配置](https://docs.timicc.com/docs/cli/6-grok-build.html)
- [TiMi API key 分组](https://docs.timicc.com/docs/register/4-token.html)
- [OpenAI Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [OpenAI reasoning models](https://developers.openai.com/api/docs/guides/reasoning)

掌握标准：能解释为何 Responses 不能复用 Chat Completions 的完整 codec，能从一次 `output_item.done` 追踪到 durable `ProviderState` 并在工具结果后原样重放，也能证明该状态不会进入 observer payload。
