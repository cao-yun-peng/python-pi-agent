# ADR 0003: Session Store with Per-Message Commit

## Status

Accepted

消息列表作为存储事实来源、用消息数量解释版本的部分已由 ADR 0004 演进为 durable event log；逐消息提交与乐观并发决定仍然有效。

## Context

`Agent` 原先只把模型可见消息保存在进程内列表中。新的 run 无法恢复旧历史，Provider 失败或任务取消后也无法重建已经发生的用户输入和工具交互。会话能力需要保持 Agent loop、存储实现和应用组装相互独立，同时避免并发写入静默覆盖历史。

## Decision

- 用 `SessionSnapshot` 表示某个版本的不可变会话视图。
- runtime 只依赖 `SessionStore` 端口；`InMemorySessionStore` 是第一份教学实现。
- `SessionStore.append()` 必须接收 `expected_version`，版本不匹配时抛出 `SessionConflictError`。
- `AgentSession` 负责加载历史、播种新会话的 system message、创建 `Agent` 并返回持久化后的结果。
- 每条完整消息在进入 Agent 内存并发出 `message_committed` 事件前，先通过异步 committer 写入存储。
- 流式 token 仍是 live event，不作为可恢复消息写入会话。
- system prompt 只在空会话中写入；恢复已有会话时以存储中的 system message 为准。

## Alternatives

- run 成功后批量保存：成功路径简单，但 Provider 失败或取消会丢失已经提交的用户消息和工具状态。
- 让 `Agent` 直接依赖 SQLite：减少一层抽象，但会把数据库生命周期和事务语义耦合到模型循环。
- 只使用进程内锁：能串行化同一个对象，却无法阻止不同实例或进程同时覆盖同一会话。
- 直接持久化流式 token：可恢复部分输出，但会让不完整 assistant message 进入模型历史并增加恢复复杂度。

## Consequences

- Provider 失败或任务取消后，已经完成持久化的消息仍可恢复。
- 每条消息产生一次存储调用；后续实现可以在保持契约的前提下用事务或日志批次优化。
- 同一 `AgentSession` 的 run 由内存锁串行化；跨实例冲突由 `expected_version` 检测。
- 当前内存实现用消息数量表示版本，适合追加式教学切片；durable event log 将使用稳定的日志序号。
- 当前能力支持恢复模型历史，但尚未实现进程重启后的持久化、事件回放和会话分叉。

## Verification

`tests/test_session_runtime.py` 验证跨 run 恢复、Provider 失败和取消后的消息保留；现有 Agent、Provider 与工具测试验证异步提交没有破坏原有循环。`examples/04_session_resume.py` 展示销毁并重新创建 `AgentSession` 后继续同一会话。
