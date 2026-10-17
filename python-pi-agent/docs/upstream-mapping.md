# Upstream Mapping

本项目是独立 Python 实现。映射用于学习设计，不承诺 API 兼容。

当前 Pi 参考固定在 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`。

| 本项目 | Pi 概念 | DeepSeek Harness 思想 | 当前差异 |
|---|---|---|---|
| `Agent.run()` | `packages/agent/src/agent-loop.ts` 的 agent loop | Runtime 消费已注册能力 | 当前只有文本，没有工具调用 |
| `ModelProvider` | `pi-ai` 的统一模型/流抽象 | Definition / Provider / Consumer capability seam | 只有最小的 `TextDelta` 与 `Finish` |
| `EventSink` | session subscribe / agent events | live events 与 durable session events 分离 | 当前没有异步事件队列 |
| committed `Message` | agent state messages | model-visible state 必须可从 durable log 重建 | 当前只在内存中，持久化待第 3 阶段 |
| `run_settled` | Pi 的 `agent_settled` 语义 | 生命周期完成事件 | 当前没有 retry/compaction/queue 工作 |
| `FakeProvider` | Pi 的 faux provider/testing patterns | 以证据验证 capability 契约 | 当前脚本只覆盖文本流 |

## 学习原则

- 学 Pi：保持 Agent Loop 小，统一供应商事件，明确 session 生命周期。
- 学 DeepSeek Harness：能力边界由 Definition、Provider、Consumer、Lifecycle 和 Evidence 共同构成。
- 不照搬：早期不拆成大量包，不引入与当前教学目标无关的动态系统。
