# Upstream Mapping

本项目是独立 Python 实现。映射用于学习设计，不承诺 API 兼容。

当前 Pi 参考固定在 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`。

| 本项目 | Pi 概念 | DeepSeek Harness 思想 | 当前差异 |
|---|---|---|---|
| `Agent.run()` | `packages/agent/src/agent-loop.ts` 的 agent/tool loop | Runtime 消费已注册能力 | 已有顺序工具循环，尚无并行 batch |
| `ModelProvider` | `pi-ai` 的统一模型/流抽象 | Definition / Provider / Consumer capability seam | 已有 DeepSeek Chat Completions，待多供应商 |
| `DeepSeekProvider` | `pi-ai` provider adapter / stream function | Provider + Lifecycle + Evidence | 可聚合流式 tool call 并回传 thinking reasoning，尚无 retry 与通用 signature 兼容层 |
| `Message.thinking` | assistant `ThinkingContent` / `thinkingSignature` | model-visible provider continuity state | 已持久化并供 DeepSeek tool turn 重放；尚无独立内容块、签名和跨模型转换 |
| `ToolDefinition` / `Tool` | `AgentTool` 与参数 schema | Capability Definition / Provider / Consumer | 使用教学型 JSON Schema 子集 |
| `ApprovalPolicy` | `beforeToolCall` block hook | 独立策略 capability | 默认拒绝受保护工具，尚无身份策略 |
| tool message | `ToolResultMessage` | model-visible durable state | 已通过 SessionStore 逐消息提交并可从 durable event replay |
| `EventSink` | session subscribe / agent events | live events 与 durable session events 分离 | 当前没有异步事件队列 |
| `AgentSession` | Pi `AgentSession` 的 prompt 与状态编排 | Consumer 组合 Provider 与存储 capability | 已支持加载、逐消息提交和恢复，待 steer/queue/compaction |
| `SessionStore` / `SessionSnapshot` | `SessionManager` 与 session entries | Definition / Provider / Consumer / Evidence | 内存与单写者 JSONL Provider 均支持持久化 fork，待多进程事务 |
| `prepare_session_append` / `fork_session_events` | session append / fork rules | Provider 共享的纯状态转换 | 内存和 JSONL 使用同一套 replay、append 与 fork 规则 |
| `JsonlSessionStore` | Pi 的 JSONL session persistence | 可替换持久化 Provider | 已有严格格式、刷盘和重启恢复，尚无文件锁与迁移工具 |
| `DurableSessionEvent` | session entries | append-only facts + replayable projection | fork 通过复制事件前缀实现；尚无父子分支元数据或压缩事件 |
| committed `Message` | agent state messages | model-visible state 必须可从 durable log 重建 | 文本、tool call、tool result 与 assistant thinking 已能回放；损坏序号会被拒绝 |
| `run_settled` | Pi 的 `agent_settled` 语义 | 生命周期完成事件 | 当前没有 retry/compaction/queue 工作 |
| `FakeProvider` | Pi 的 faux provider/testing patterns | 以证据验证 capability 契约 | 已覆盖文本与完整 ToolCall |

## 学习原则

- 学 Pi：保持 Agent Loop 小，统一供应商事件，明确 session 生命周期。
- 学 DeepSeek Harness：能力边界由 Definition、Provider、Consumer、Lifecycle 和 Evidence 共同构成。
- 不照搬：早期不拆成大量包，不引入与当前教学目标无关的动态系统。
