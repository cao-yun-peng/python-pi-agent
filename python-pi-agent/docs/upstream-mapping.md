# Upstream Mapping

本项目是独立 Python 实现。映射用于学习设计，不承诺 API 兼容。

当前 Pi 参考固定在 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`。

| 本项目 | Pi 概念 | DeepSeek Harness 思想 | 当前差异 |
|---|---|---|---|
| `Agent.run()` | `packages/agent/src/agent-loop.ts` 的 agent/tool loop | Runtime 消费已注册能力 | 已有顺序工具循环，尚无并行 batch |
| `ModelProvider` | `pi-ai` 的统一模型/流抽象 | Definition / Provider / Consumer capability seam | 已有 DeepSeek Chat Completions，待多供应商 |
| `DeepSeekProvider` | `pi-ai` provider adapter / stream function | Provider + Lifecycle + Evidence | 可聚合流式 tool call 并回传 thinking reasoning，尚无 retry 与通用 signature 兼容层 |
| `Message.thinking` | assistant `ThinkingContent` / `thinkingSignature` | model-visible provider continuity state | 已持久化并供 DeepSeek tool turn 重放；尚无独立内容块、签名和跨模型转换 |
| `ToolDefinition` / `Tool` | `AgentTool`、参数 schema 与 execute context | Capability Definition / Provider / Consumer | 使用教学型 JSON Schema 子集；handler 接收隔离快照 context |
| `ToolExecutionContext` | core toolCallId/params/signal + coding-agent `ExtensionToolContext` / `BeforeToolCallContext` | invocation-scoped capability context | 已含 call/definition、session、request/call index 与审批决定；待 signal API、cwd、identity、nested calls 和 progress |
| `ApprovalPolicy` | `beforeToolCall` block hook | 独立策略 capability | 使用完整 context，异常/错误返回值 fail-closed；默认拒绝受保护工具，尚无身份策略与 durable approval |
| `Sandbox` / `DeclaredSideEffectSandbox` | coding-agent 工具工厂与进程执行边界的应用侧组合 | policy + execution provider seam | runtime 只经 Sandbox 调度；默认只允许 none effect。当前 provider 是进程内声明式闸门，不是 OS 隔离 |
| tool message | `ToolResultMessage` | model-visible durable state | 已通过 SessionStore 逐消息提交并可从 durable event replay；重启时会保守补齐尾部未决调用，副作用结果标记为 unknown |
| `EventSink` / `IsolatedEventSink` | `Agent.subscribe()` / `AgentSession.subscribe()` | live events 与 durable session events 分离 | Pi core Agent 会 await listener；本项目把 EventSink 限定为观察者并自动隔离异常，拦截行为留给 Hook |
| `RedactingEventSink` / `BufferedEventSink` | UI/session listener 的应用侧消费 | telemetry 数据最小化 + lifecycle | 默认隐藏模型可见内容后再进入有界队列；best-effort、显式关闭，不替代 durable log |
| `AgentSession` | Pi `AgentSession` 的 prompt/steer/followUp/abort、状态与 compaction 编排 | Consumer 组合 Provider、存储与控制 capability | 已支持 FIFO dispatcher、steer、follow-up、abort、settled/idle、恢复和压缩；控制队列暂不跨进程持久化 |
| `NextTurnHook` | `runLoop()` 的 `prepareNextTurn` + `AgentSession._compactBeforeNextAssistantResponse()` | runtime interceptor capability seam | Agent 保持单插槽，PluginHost 可在插槽外组合多个 Hook；仍无队列与模型路由 |
| `PluginFactory` / `PluginAPI` | `ExtensionFactory` / `ExtensionAPI` | 显式 capability registration | 当前只注册 Tool、NextTurnHook 与 cleanup，不含 UI/命令/Provider/MCP |
| `PluginHost` | extension loader + `ExtensionRunner` | Provider lifecycle + evidence | staging 后原子发布、冲突失败、逆序释放；不做目录发现与热重载 |
| plugin hook chain | ExtensionRunner 的顺序 handler dispatch | ordered interceptor pipeline | 使用 priority + 注册顺序、逐 Hook 超时和 fail-closed；Pi 的许多 handler 会记录错误后继续 |
| `Registration` | `pi.on()` 返回的 unsubscribe | reversible explicit side effect | Tool、Hook、cleanup 注册都返回幂等撤销句柄 |
| `ContextPlan` / `ContextStrategy` | `packages/agent/src/agent-loop.ts` 的 `runLoop()` 在模型调用前执行 `transformContext` | Definition / Provider / Consumer capability seam | 已分离 durable history 与请求投影，当前默认仍发送完整历史 |
| `TokenEstimator` / `TokenBudgetContextStrategy` | `packages/coding-agent/src/core/compaction/compaction.ts` 的 `estimateTokens()` / `findCutPoint()` | 可替换估算 Provider + 策略 Consumer | 同样使用字符启发式；本项目按完整 user turn 裁剪，不允许 split turn |
| `context_prepared` | compaction/context 生命周期事件 | capability evidence | 仅记录数量与预算元数据，不持久化提示词正文 |
| `CompactionStrategy` / `ModelCompactionStrategy` | `compaction.ts` 的 `compact()` 与 summary generation | 可替换 Definition / Provider / Consumer | 使用独立 ModelProvider，不暴露工具；待增量摘要与质量 eval |
| `CompactionPolicy` / `TokenThresholdCompactionPolicy` | Pi threshold/overflow check / OpenAI `compact_threshold` | 触发 Definition + runtime Consumer + trace Evidence | run 前估算 persisted history + incoming prompt，并在每个工具续轮前重评；尚无 overflow recovery |
| `SessionCompaction` / `context_compacted` | `CompactionEntry` 与 session context reconstruction | durable capability evidence | 保存前缀替换操作并由 replay 应用；当前不支持 split-turn 和文件跟踪 |
| `SessionStore` / `SessionSnapshot` | `SessionManager` 与 session entries | Definition / Provider / Consumer / Evidence | 内存与单写者 JSONL Provider 均支持持久化 fork 和尾部未决工具调用恢复，待 durable tool-started 事实与多进程事务 |
| `prepare_session_append` / `prepare_session_compaction` / `fork_session_events` | session append / compaction / fork rules | Provider 共享的纯状态转换 | 内存和 JSONL 使用同一套 replay、append、compact 与 fork 规则 |
| `JsonlSessionStore` | Pi 的 JSONL session persistence | 可替换持久化 Provider | schema v4 支持压缩、event/run/turn/provenance、严格格式、刷盘和 v1-v3 兼容读取，尚无跨进程文件锁与迁移工具 |
| `DurableSessionEvent` | session entries + run/turn lifecycle correlation | append-only facts + replayable projection | 每个事实有稳定 event ID、run/turn 和来源；fork 通过 copied_from_event_id 保留来源 |
| committed `Message` | agent state messages | model-visible state 必须可从 durable log 重建 | 文本、tool call、tool result 与 assistant thinking 已能回放；损坏序号会被拒绝 |
| `run_settled` / `AgentSession.wait_until_idle()` | Pi `agent_settled` / `waitForIdle()` | per-run lifecycle + session control barrier | 明确区分单 run settled 与无活动/无 follow-up 的 session idle |
| `FakeProvider` | Pi 的 faux provider/testing patterns | 以证据验证 capability 契约 | 已覆盖文本与完整 ToolCall |

## 学习原则

- 学 Pi：保持 Agent Loop 小，统一供应商事件，明确 session 生命周期。
- 学 DeepSeek Harness：能力边界由 Definition、Provider、Consumer、Lifecycle 和 Evidence 共同构成。
- 不照搬：早期不拆成大量包，不引入与当前教学目标无关的动态系统。
