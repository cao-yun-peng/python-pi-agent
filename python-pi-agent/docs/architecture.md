# Architecture

## 当前边界

```text
domain.py <- ports.py <- runtime.py <- examples / future apps
               ^           |
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
- `runtime.AgentSession` 加载会话历史，并通过逐消息异步提交把 Agent loop 连接到 `SessionStore`。
- `session_transitions.py` 集中实现 replay、append 校验、乐观版本检查和事件生成，不执行 I/O 或修改调用者状态。
- `session.py` 提供 `InMemorySessionStore`，只负责内存事件容器。
- `session_jsonl.py` 提供带格式版本、严格解码和刷盘语义的 `JsonlSessionStore`。
- `events.py` 提供最小观察适配器。
- `testing.py` 是确定性 Provider 实现，也是 Provider 契约的第一份证据。
- `providers/deepseek.py` 隔离 DeepSeek 的 HTTP、认证、SSE 和错误格式。
- `tools.py` 提供 `FunctionTool`、审批策略和可替换的 schema 校验边界。

## 状态模型

当前 durable event log 是会话事实来源，`SessionSnapshot.messages` 是通过 replay 得到的模型可见状态投影。用户消息在请求前提交；assistant 消息只有在收到明确的 `Finish` 后才提交。流式文本仅通过 live event 暴露。

工具调用以带 `tool_calls` 的 assistant message 持久存在，工具结果以引用 `tool_call_id` 的 tool message 回填。schema 错误、审批拒绝和执行失败也是模型可见结果；外部取消则终止整个 run。

`AgentSession` 通过 `SessionStore.load()` 恢复历史，并在每条完整消息进入 Agent 内存和触发 `message_committed` 前先调用 `SessionStore.append()`。两个 Store 都调用 `prepare_session_append()` 获得不可变 `SessionAppendPlan`，其中包含待保存事件和保存后的 snapshot；Provider 只负责提交计划。序号断裂或混入其他 session 的事件会导致 `SessionReplayError`，避免从损坏日志静默恢复。

`JsonlSessionStore` 把多个 session 的事件写入同一个 JSONL 文件，每行包含 `schema_version` 和一个完整事件。读取、反序列化和 replay 仍使用相同领域对象，因此 runtime 无需感知存储变化。当前实现通过实例内锁串行访问，并在追加后执行 `flush()` 与 `fsync()`；它支持进程重启后的恢复，但尚不支持多个进程同时写入同一文件。

## 依赖规则

- domain 不依赖 ports 或 runtime。
- ports 只依赖 domain。
- runtime 依赖 domain 和 ports，不依赖具体 Provider。
- session_transitions 只依赖领域对象和稳定错误，不依赖存储 Provider。
- session 与 session_jsonl 实现 ports 中的 `SessionStore`，ports 不依赖具体存储。
- apps 负责显式组装，不通过 import 副作用注册。
- runtime 顺序执行工具；并行策略要等资源隔离与取消语义稳定后再加入。
