# Architecture

## 当前边界

```text
domain.py <- ports.py <- runtime.py <- examples / future apps
               ^           |
               |           ├── context.py implements ContextStrategy
               |           ├── compaction.py implements CompactionStrategy
               |           ├── plugins.py assembles Tool / NextTurnHook
               |           └── tools.py provides tools, approval, and sandbox providers
               ├── events.py implements isolation, redaction, and buffering
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
- `events.py` 提供同步观察适配器、runtime 失败隔离、日志脱敏和显式关闭的有界后台投递。
- `testing.py` 是确定性 Provider 实现，也是 Provider 契约的第一份证据。
- `providers/deepseek.py` 隔离 DeepSeek 的 HTTP、认证、SSE 和错误格式。
- `tools.py` 提供 `FunctionTool`、审批策略、声明式副作用 Sandbox 和可替换的 schema 校验边界。
- `plugins.py` 提供显式、事务化的 `PluginHost`，把 Tool、Hook 与 cleanup 组合成不可变能力快照。

## 状态模型

当前 durable event log 是会话事实来源，`SessionSnapshot.messages` 是通过 replay 得到的模型可见状态投影。用户消息在请求前提交；assistant 消息只有在收到明确的 `Finish` 后才提交。流式文本和 thinking delta 立即通过 live event 暴露；完成后的 thinking 单独保存在 `Message.thinking`，不混入面向用户的正文，但可供供应商协议在后续请求中重放。

每个 durable event 具有稳定的 `event_id=<session_id>:<sequence>`、可选 `run_id`、可选 `turn_id` 和强类型 `EventProvenance`。一次排队任务对应一个 run；每次模型请求及其工具结果对应 `<run_id>:turn:<request_number>`。user/model/tool/recovery/compaction 的来源在提交边界声明，live/capability event 同时携带当前 run/turn 便于关联，但不能替代 durable log。fork 为目标会话重新生成 event ID，并用 `copied_from_event_id` 保留源事件关系。

工具调用以带 `tool_calls` 的 assistant message 持久存在，工具结果以引用 `tool_call_id` 的 tool message 回填。schema 错误、审批拒绝和执行失败也是模型可见结果；外部取消则终止整个 run。

工具安全管线固定为 resolve → validate → approval context → ApprovalPolicy → execution context → Sandbox → Tool。`ToolExecutionContext` 携带 call/definition、session id、模型请求序号、批次位置和可选审批决定。审批与执行分别从原始 committed call 建立深拷贝快照，防止 policy 或 handler 修改嵌套 JSON 后污染 transcript，或把审批参数偷换成执行参数。受保护工具在审批异常、错误返回值或无策略时 fail-closed。

runtime 只通过 `Sandbox.execute()` 调度工具，不再直接调用 `Tool.execute()`。默认 `DeclaredSideEffectSandbox` 只允许 `side_effects="none"`，应用必须显式允许 read/write/external，并可叠加工具名 allowlist；Sandbox 还会二次确认受保护调用携带批准决定。默认实现与 `InProcessSandbox` 都在当前 Python 进程中执行，只是可替换的策略/调度边界，不构成 OS 级隔离。对不可信工具必须提供子进程、容器或远端 Sandbox provider。沙箱拒绝产生 `tool_sandbox_denied` 和模型可见 error tool result；timeout 包围整个 sandbox/tool 调用，取消继续传播。

逐消息提交允许取消或进程退出发生在一个工具批次中间。`AgentSession` 每次继续运行或手动压缩前都会校验工具调用/结果序列：若会话尾部存在未决调用，就为每个缺失结果追加一个 `is_error=True` 的恢复 tool message，明确记录“外部副作用结果未知，不得自动重试”，然后才允许追加新的用户消息。已经完成的 tool result 不会重复生成。若未决调用之后已经存在其他 user/assistant 消息，append-only 日志无法在原位置安全修补，session 会以 `SessionValidationError` 拒绝继续，而不是把错误结果追加到错误位置。恢复消息本身是普通 `message_committed` durable fact，因此内存、JSONL、fork 和 replay 使用同一语义；`tool_call_recovered` capability event 提供恢复证据。

`AgentSession` 通过 `SessionStore.load()` 恢复历史，并在每条完整消息进入 Agent 内存和触发 `message_committed` 前先调用 `SessionStore.append()`。两个 Store 都调用 `prepare_session_append()` 获得不可变 `SessionAppendPlan`，其中包含待保存事件和保存后的 snapshot；Provider 只负责提交计划。序号断裂或混入其他 session 的事件会导致 `SessionReplayError`，避免从损坏日志静默恢复。`SessionSnapshot.version` 始终表示已应用 durable event 数量，不等于压缩后消息数量。

`AgentSession` 的控制面使用显式 FIFO dispatcher，而不是把并发请求隐式阻塞在互斥锁上。公开状态为 idle/running/aborting/settled；`run()` 等待自己的排队任务，`follow_up()` 返回新 run ID 并在前序 run settled 后执行，`wait_until_idle()` 等待活动任务和 follow-up 队列都清空。`steer()` 只在活动 run 的接收窗口开放时成功，在当前模型/工具 turn 的安全边界提交为 user message后继续同一 run。最终 turn 检查空队列与关闭接收窗口共用控制锁，保证成功入队的 steer 不会因结束竞态丢失。`abort()` 默认取消活动任务、清理 steer/follow-up 并等待 idle；已提交 durable facts 不回滚。

每次模型请求前，runtime 把完整 committed history 交给 `ContextStrategy`，并只把返回的 `ContextPlan.messages` 发送给 `ModelProvider`。默认策略不省略消息。`context_prepared` capability event 暴露源消息数、选中数和省略数，但不复制消息正文。策略投影不会删除或改写 `Agent.messages` 与 durable log。

预算策略通过可替换 `TokenEstimator` 计算投影大小。system 消息固定保留；从 user 到下一个 user 之前的 assistant tool call、tool result 与最终回答组成不可拆分 turn。策略保留连续的最近 turn，mandatory context 超预算时显式失败，不截断当前输入或工具链。

durable compaction 可由 `AgentSession.compact()` 显式触发，也可在配置 `CompactionPolicy` 后自动触发。run 前，`TokenThresholdCompactionPolicy` 估算 persisted history 加 incoming prompt；同一工具循环内，Agent 在所有 tool result 提交后、下一次模型请求前调用 `NextTurnHook`，`AgentSession` 的适配器再次估算当前 durable snapshot。达到阈值后，`prepare_compaction()` 保留 leading system messages 与指定数量的近期完整 turn，`CompactionStrategy` 只为旧前缀生成摘要。摘要成功后，Store 以乐观版本检查追加一个 `context_compacted` event；replay 将指定前缀替换为带标记的 summary message，Hook 再把 replay 后的 canonical history 返回给 Agent。旧消息事件不删除，所以失败与取消不会产生半成品，重启与 fork 仍走同一 replay 规则；compaction 仍不减少日志磁盘占用。

`SessionStore.fork()` 从源日志的指定 version 创建一个新的目标 session。两个 Store 都调用 `fork_session_events()` 复制经过 replay 校验的历史前缀，拒绝覆盖已有目标，并允许源和目标在分叉后独立追加。当前事件模型没有 `session_created` 元数据，因此持久化 fork 必须至少包含一个事件。

`JsonlSessionStore` 把多个 session 的事件写入同一个 JSONL 文件，每行包含 `schema_version` 和一个完整事件。schema v2 加入 assistant thinking，schema v3 加入 message/compaction 互斥 payload，schema v4 加入 event/run/turn/provenance；解码器仍可读取 v1-v3 日志并标记 legacy provenance。读取、反序列化和 replay 使用相同领域对象，因此 runtime 无需感知存储变化。append、compact 与 fork 都通过实例内锁串行执行，写入后调用 `flush()` 与 `fsync()`；它支持进程重启后的恢复和分支恢复，但尚不支持多个进程同时写入同一文件。

插件系统采用显式 assembly，而不是目录扫描或 import 副作用。`PluginFactory` 通过受限 `PluginAPI` 注册工具、有序 `NextTurnHook` 和 cleanup；`PluginHost.load()` 先 staging，factory 与冲突校验全部成功后再原子发布。工具重名会拒绝整个插件。Hook 按 priority 与注册顺序串行组合，带独立超时，失败关闭并保留取消传播。卸载先撤销注册，再反向执行 cleanup。`PluginCapabilities` 是组装时快照，当前不支持运行中的热重载。

插件 Hook 暂不直接接入 `AgentSession`。普通 `Agent` 可以消费快照中的 Hook；有 durable store 的会话若允许任意 Hook 改写 canonical history，会导致内存上下文与 replay 分叉。后续 session-aware 扩展只能修改请求投影，或产生可持久化的 session transition。

观察与拦截使用不同错误语义。`Agent`、`AgentSession` 和 `PluginHost` 自动把应用的 `EventSink` 包进 `IsolatedEventSink`；sink 异常只产生有界 `EventSinkFailure`，不改变模型、工具、持久化或插件控制流，也不会覆盖原始错误。失败诊断不保存异常消息，避免把密钥或用户数据从异常文本二次写入日志。正常同步 sink 仍保持立即、有序投递，已有 UI 和测试不需要异步化。

日志安全由应用显式组装，而不是破坏原始事件。`RedactingEventSink` 复制事件，默认隐藏 message/text/thinking、工具参数、结构化结果和嵌套敏感键；token 用量元数据保留。慢 I/O 可放入 `BufferedEventSink` 的有界队列，由单 worker 顺序送往同步 logger。推荐顺序是 `Isolated(runtime) -> Redacting -> Buffered -> logger`，使数据在入队前已脱敏。队列满或 logger 失败允许丢失并记录诊断，因此 EventSink 仍是 best-effort 观察通道，不能替代 `SessionStore` durable log。后台 worker 必须通过 `aclose()` 显式排空和关闭。

## 依赖规则

- domain 不依赖 ports 或 runtime。
- ports 只依赖 domain。
- runtime 依赖 domain 和 ports，不依赖具体 Provider。
- runtime 消费 `ContextStrategy` 端口；默认实现位于 context，策略不得反向修改 durable history。
- runtime 消费 `CompactionStrategy` 端口；策略只生成摘要，持久化由 `SessionStore` 控制。
- runtime 消费 `CompactionPolicy` 端口；policy 只决定何时触发，不生成摘要或写存储。
- Agent loop 消费 `NextTurnHook` 端口；Hook 可以在安全边界替换下一轮 canonical history，但 SessionStore 与 compaction 细节只存在于 `AgentSession` 适配器。
- session_transitions 只依赖领域对象和稳定错误，不依赖存储 Provider。
- session_transitions 校验 tool call/result 批次，只把尾部缺失结果暴露为可追加恢复项；非尾部缺口显式失败。
- session 与 session_jsonl 实现 ports 中的 `SessionStore`，ports 不依赖具体存储。
- apps 负责显式组装，不通过 import 副作用注册。
- PluginHost 只组合端口实现，Agent/runtime 不反向依赖插件容器。
- 插件拦截 Hook 失败关闭；观察型 EventSink 由 runtime 隔离失败，不得改变控制流。
- 日志适配器只能消费事件副本；脱敏不得修改模型可见消息或 durable state。
- BufferedEventSink 是可丢失的观察队列，不能承载恢复、审计或业务提交事实。
- runtime 顺序执行工具；并行策略要等资源隔离与取消语义稳定后再加入。
- ApprovalPolicy 只作决定，Sandbox 独占最终执行入口；用户批准不会自动扩大 sandbox allowlist。
- ToolExecutionContext 是运行时快照而非 durable fact；跨进程人工审批需要独立持久状态机。
- 尚未消费的 steer/follow-up 是进程内控制状态；消费后才成为 durable message。跨进程队列需要独立 claim/ack/lease 协议。
