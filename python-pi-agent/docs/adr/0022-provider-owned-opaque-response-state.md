# ADR 0022：持久化 Provider-owned Opaque Responses State

## 状态

Accepted

## 背景

ADR 0006 用 `Message.thinking` 解决了 Chat Completions 中可读 reasoning 文本的续轮问题。OpenAI Responses API 的无服务端存储模式不同：reasoning 模型会返回包含 `encrypted_content` 的 output item，function call 也以 output item 表示；工具结果之后必须把这些 item 原样放回下一次 `input`。只保存可读 thinking 或规范化 `ToolCall` 无法重建这一协议状态。

TiMi CC 的 Codex/GPT/Grok 接入统一使用 Responses API。本项目不希望 Provider 在内存里维护隐藏会话，也不希望 runtime 认识 `response.output_item.done`。

## 决定

- 增加 `ProviderState(provider, item)` 领域值和 assistant-only 的 `Message.provider_state` 元组。
- 具体 Provider 拥有 item schema；runtime 只校验外层 JSON object、深拷贝、随完整 assistant message 提交，并忽略所属 Provider 的具体字段。
- `TimiccResponsesProvider` 固定使用 `store: false` 和 `include: ["reasoning.encrypted_content"]`，保存 completed output item，并在下一次请求中只重放属于 `timicc-responses` 的 item。
- provider state 是模型可见 durable state，但不进入 best-effort observer payload。需要审计会话的存储边界仍必须像保护消息正文一样保护 JSONL。
- JSONL schema 升级到 v6；v1-v5 解码时该字段默认为空。
- Responses 与 Chat Completions 只共享 `_sse.py` 的 framing 和无状态 support helper，不建立 Provider 基类，也不共享消息、工具分片或完成事件 codec。

## 备选方案

- Provider 内存缓存：拒绝。重启、fork 和 replay 会丢失模型可见状态，并形成进程级隐式会话。
- 把 encrypted item 放入 `Message.thinking`：拒绝。它不是可读文本，且 function call/output item 无法用字符串字段完整表达。
- 把 Responses 分支加入 `_openai_compat.py`：拒绝。两种 API 的消息、工具、usage 和终止语义不同，只共享 SSE framing；合并会形成条件不断扩张的伪抽象。
- 使用 `previous_response_id` 和服务端存储：暂缓。它把恢复依赖外部保留策略与网络状态，不满足当前本地 durable replay 的教学目标。

## 后果

- Agent loop 仍消费统一事件，不含 TiMi/OpenAI 名称；JSONL 重启后可以继续 stateless reasoning/tool loop。
- context 预算估算会计算 opaque item 的序列化字符数，避免它成为不可见的零成本上下文。
- durable log 会保存密文形式的 provider state，必须限制访问和保留；普通 observer/logger 默认看不到该字段。
- 切换 Provider 时，目标适配器忽略不属于自己的 state；当前不定义跨模型转换。

## 验证方式

- MockTransport 验证 typed SSE、function argument 分片、completed output item、usage 和 finish 映射。
- 第二轮请求断言 reasoning/function_call item 与 tool output 的顺序和内容原样重放。
- runtime 测试断言 state 被提交但 observer payload 与 repr 不出现密文。
- JSONL v6 round-trip 与 v1-v5 fixture 验证恢复兼容。
- timeout、取消、HTTP 错误、incomplete 和缺失 terminal event 均有离线失败路径。
