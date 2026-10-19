# Upstream Mapping

本项目是独立 Python 实现。映射用于学习设计，不承诺 API 兼容。

当前 Pi 参考固定在 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`。

| 本项目 | Pi 概念 | DeepSeek Harness 思想 | 当前差异 |
|---|---|---|---|
| `Agent.run()` | `packages/agent/src/agent-loop.ts` 的 agent/tool loop | Runtime 消费已注册能力 | 已有顺序工具循环，尚无并行 batch |
| `ModelProvider` | `pi-ai` 的统一模型/流抽象 | Definition / Provider / Consumer capability seam | 已有 DeepSeek Chat Completions，待多供应商 |
| `DeepSeekProvider` | `pi-ai` provider adapter / stream function | Provider + Lifecycle + Evidence | 可聚合流式 tool call，尚无 retry |
| `ToolDefinition` / `Tool` | `AgentTool` 与参数 schema | Capability Definition / Provider / Consumer | 使用教学型 JSON Schema 子集 |
| `ApprovalPolicy` | `beforeToolCall` block hook | 独立策略 capability | 默认拒绝受保护工具，尚无身份策略 |
| tool message | `ToolResultMessage` | model-visible durable state | 已通过 SessionStore 逐消息提交，待 durable event replay |
| `EventSink` | session subscribe / agent events | live events 与 durable session events 分离 | 当前没有异步事件队列 |
| `AgentSession` | Pi `AgentSession` 的 prompt 与状态编排 | Consumer 组合 Provider 与存储 capability | 已支持加载、逐消息提交和恢复，待 steer/queue/compaction |
| `SessionStore` / `SessionSnapshot` | `SessionManager` 与 session entries | Definition / Provider / Consumer / Evidence | 已有内存实现和乐观版本，待事件日志、分叉和进程级持久化 |
| committed `Message` | agent state messages | model-visible state 必须可从 durable log 重建 | 已能从 SessionStore 恢复，尚未从事件日志回放 |
| `run_settled` | Pi 的 `agent_settled` 语义 | 生命周期完成事件 | 当前没有 retry/compaction/queue 工作 |
| `FakeProvider` | Pi 的 faux provider/testing patterns | 以证据验证 capability 契约 | 已覆盖文本与完整 ToolCall |

## 学习原则

- 学 Pi：保持 Agent Loop 小，统一供应商事件，明确 session 生命周期。
- 学 DeepSeek Harness：能力边界由 Definition、Provider、Consumer、Lifecycle 和 Evidence 共同构成。
- 不照搬：早期不拆成大量包，不引入与当前教学目标无关的动态系统。
