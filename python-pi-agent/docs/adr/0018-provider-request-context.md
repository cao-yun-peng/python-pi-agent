# ADR 0018: Provider Request Context

## 背景

`ModelProvider.stream(messages, tools)` 只能看到请求内容，无法判断调用属于哪个 session/run/turn、是普通 Agent turn 还是 compaction、也无法稳定关联 retry/fallback 与 telemetry。把这些信息放进全局变量会破坏并发 Session 隔离。

## 决定

- 每次 Provider 调用必须接收不可变 `ProviderRequestContext`。
- `request_id` 标识逻辑请求，`attempt_id` 标识一次物理尝试，`attempt` 为重试序号。
- context 包含 purpose、request number，以及可选 session/run/turn、父请求和因果事件 ID。
- `agent_turn` 必须有 run/turn；standalone Agent 可以没有 session。
- compaction 使用 `purpose="compaction"`，继续明确传空工具列表。
- FakeProvider 保存 context 作为确定性测试证据；Provider context 不包含密钥、headers 或 prompt 副本。

## 备选方案

- 从 messages 或日志猜当前调用：无法区分内容相同的请求，也无法可靠识别 compaction。
- Provider 读取 AgentSession 全局状态：反转依赖并破坏多 Session 并发。
- 使用无结构 metadata 字典：字段和敏感数据边界无法校验。

## 后果

ModelProvider 与 CompactionStrategy 端口发生显式签名变化，所有 Provider、fake 和测试替身必须适配。当前还没有 retry/router；这些能力以后复用 request/attempt 身份，不需要再次改变主循环上下文契约。

## 验证

- standalone Agent 获得 run/turn 和 `purpose=agent_turn`。
- AgentSession 请求同时携带 session ID，连续 run 使用不同 run ID。
- 手动与自动 compaction 获得 `purpose=compaction`，自动压缩与目标 run 关联。
- DeepSeek MockTransport 与所有取消/失败测试继续通过。
