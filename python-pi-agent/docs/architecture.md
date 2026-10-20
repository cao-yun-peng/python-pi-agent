# Architecture

## 当前边界

```text
domain.py <- ports.py <- runtime.py <- examples / future apps
               ^           |
               |           ├── context.py implements ContextStrategy
               |           ├── compaction.py implements CompactionStrategy
               |           └── tools.py provides policies and FunctionTool
               ├── session_transitions.py provides pure state transitions
               ├── session.py implements SessionStore
               ├── session_jsonl.py implements persistent SessionStore
               ├── testing.py implements ModelProvider
               └── providers/deepseek.py implements ModelProvider
```

- `domain.py` 只包含无 I/O 的值对象。
- `ports.py` 定义核心运行时所需能力，不提供供应商实现。
- `runtime.py` 消费端口并维护已提交消息。
- `context.py` 提供全历史与 token budget 两种 `ContextStrategy`、字符估算器及完整 turn 分组逻辑。
- `compaction.py` 提供 token threshold policy、选择可安全替换的完整旧 turn，并通过可替换模型 Provider 生成摘要。
- `runtime.AgentSession` 加载会话历史，并通过逐消息异步提交把 Agent loop 连接到 `SessionStore`。
- `session_transitions.py` 集中实现 replay、append 与 fork 的校验和事件生成，不执行 I/O 或修改调用者状态。
- `session.py` 提供 `InMemorySessionStore`，只负责内存事件容器。
- `session_jsonl.py` 提供带格式版本、严格解码和刷盘语义的 `JsonlSessionStore`。
- `events.py` 提供最小观察适配器。
- `testing.py` 是确定性 Provider 实现，也是 Provider 契约的第一份证据。
- `providers/deepseek.py` 隔离 DeepSeek 的 HTTP、认证、SSE 和错误格式。
- `tools.py` 提供 `FunctionTool`、审批策略和可替换的 schema 校验边界。

## 状态模型

当前 durable event log 是会话事实来源，`SessionSnapshot.messages` 是通过 replay 得到的模型可见状态投影。用户消息在请求前提交；assistant 消息只有在收到明确的 `Finish` 后才提交。流式文本和 thinking delta 立即通过 live event 暴露；完成后的 thinking 单独保存在 `Message.thinking`，不混入面向用户的正文，但可供供应商协议在后续请求中重放。

工具调用以带 `tool_calls` 的 assistant message 持久存在，工具结果以引用 `tool_call_id` 的 tool message 回填。schema 错误、审批拒绝和执行失败也是模型可见结果；外部取消则终止整个 run。

`AgentSession` 通过 `SessionStore.load()` 恢复历史，并在每条完整消息进入 Agent 内存和触发 `message_committed` 前先调用 `SessionStore.append()`。两个 Store 都调用 `prepare_session_append()` 获得不可变 `SessionAppendPlan`，其中包含待保存事件和保存后的 snapshot；Provider 只负责提交计划。序号断裂或混入其他 session 的事件会导致 `SessionReplayError`，避免从损坏日志静默恢复。`SessionSnapshot.version` 始终表示已应用 durable event 数量，不等于压缩后消息数量。

每次模型请求前，runtime 把完整 committed history 交给 `ContextStrategy`，并只把返回的 `ContextPlan.messages` 发送给 `ModelProvider`。默认策略不省略消息。`context_prepared` capability event 暴露源消息数、选中数和省略数，但不复制消息正文。策略投影不会删除或改写 `Agent.messages` 与 durable log。

预算策略通过可替换 `TokenEstimator` 计算投影大小。system 消息固定保留；从 user 到下一个 user 之前的 assistant tool call、tool result 与最终回答组成不可拆分 turn。策略保留连续的最近 turn，mandatory context 超预算时显式失败，不截断当前输入或工具链。

durable compaction 可由 `AgentSession.compact()` 显式触发，也可在配置 `CompactionPolicy` 后自动触发。run 前，`TokenThresholdCompactionPolicy` 估算 persisted history 加 incoming prompt；同一工具循环内，Agent 在所有 tool result 提交后、下一次模型请求前调用 `NextTurnHook`，`AgentSession` 的适配器再次估算当前 durable snapshot。达到阈值后，`prepare_compaction()` 保留 leading system messages 与指定数量的近期完整 turn，`CompactionStrategy` 只为旧前缀生成摘要。摘要成功后，Store 以乐观版本检查追加一个 `context_compacted` event；replay 将指定前缀替换为带标记的 summary message，Hook 再把 replay 后的 canonical history 返回给 Agent。旧消息事件不删除，所以失败与取消不会产生半成品，重启与 fork 仍走同一 replay 规则；compaction 仍不减少日志磁盘占用。

`SessionStore.fork()` 从源日志的指定 version 创建一个新的目标 session。两个 Store 都调用 `fork_session_events()` 复制经过 replay 校验的历史前缀，拒绝覆盖已有目标，并允许源和目标在分叉后独立追加。当前事件模型没有 `session_created` 元数据，因此持久化 fork 必须至少包含一个事件。

`JsonlSessionStore` 把多个 session 的事件写入同一个 JSONL 文件，每行包含 `schema_version` 和一个完整事件。schema v2 加入 assistant thinking，schema v3 加入 message/compaction 互斥 payload；解码器仍可读取 v1/v2 消息日志。读取、反序列化和 replay 使用相同领域对象，因此 runtime 无需感知存储变化。append、compact 与 fork 都通过实例内锁串行执行，写入后调用 `flush()` 与 `fsync()`；它支持进程重启后的恢复和分支恢复，但尚不支持多个进程同时写入同一文件。

## 依赖规则

- domain 不依赖 ports 或 runtime。
- ports 只依赖 domain。
- runtime 依赖 domain 和 ports，不依赖具体 Provider。
- runtime 消费 `ContextStrategy` 端口；默认实现位于 context，策略不得反向修改 durable history。
- runtime 消费 `CompactionStrategy` 端口；策略只生成摘要，持久化由 `SessionStore` 控制。
- runtime 消费 `CompactionPolicy` 端口；policy 只决定何时触发，不生成摘要或写存储。
- Agent loop 消费 `NextTurnHook` 端口；Hook 可以在安全边界替换下一轮 canonical history，但 SessionStore 与 compaction 细节只存在于 `AgentSession` 适配器。
- session_transitions 只依赖领域对象和稳定错误，不依赖存储 Provider。
- session 与 session_jsonl 实现 ports 中的 `SessionStore`，ports 不依赖具体存储。
- apps 负责显式组装，不通过 import 副作用注册。
- runtime 顺序执行工具；并行策略要等资源隔离与取消语义稳定后再加入。
