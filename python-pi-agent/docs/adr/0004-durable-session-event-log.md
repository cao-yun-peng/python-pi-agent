# ADR 0004: Durable Session Event Log as Source of Truth

## Status

Accepted

## Context

ADR 0003 引入了逐消息提交和可恢复快照，但内存 Provider 仍直接保存消息列表。该表示可以恢复当前上下文，却没有明确区分不可变事实与派生状态，也无法校验日志是否缺失、乱序或串入其他会话的数据。

## Decision

- 用 `DurableSessionEvent` 表示按 session 排序的持久事实。
- 当前只定义 `message_committed`，每个事件保存一条完整 `Message`。
- `sequence` 从 1 开始且必须连续；`SessionSnapshot.version` 等于最后成功回放的序号。
- `replay_session()` 是无 I/O 的纯函数，从有序事件生成 `SessionSnapshot`。
- replay 遇到序号断裂或 session 不匹配时抛出 `SessionReplayError`，不生成部分可信快照。
- `InMemorySessionStore` 以事件列表为事实来源；`load()` 每次通过 replay 生成快照。
- `AgentEvent` 继续承担运行时观察通知，不能替代存储中的 `DurableSessionEvent`。

## Alternatives

- 继续直接保存最新消息列表：实现更短，但无法证明历史完整性，也模糊了快照与事实的区别。
- 直接引入通用 event-sourcing 框架：可获得持久化、快照和订阅能力，但会掩盖本阶段要学习的追加与回放机制。
- 复用运行时 `AgentEvent`：减少一个类型，却把 UI/trace 通知与可恢复数据耦合，并且其 payload 契约不足以稳定重建消息。
- 遇到坏事件时跳过：提高表面可用性，但会让模型在不完整上下文上继续执行，风险不可接受。

## Consequences

- 快照与 Agent 内存都成为可丢弃、可重建的投影。
- 日志损坏会显式失败，调用方可以区分存储读取成功与安全回放成功。
- 当前 append 和 load 都是 O(n)，适合教学规模；未来可以加入经过验证的快照缓存而不改变事件语义。
- 目前一个事件对应一条消息，但未来的分叉、压缩或元数据事件可以让事件数与消息数不同。
- 内存 Provider 仍不能跨进程恢复；磁盘 durability 属于后续 Provider 的职责。

## Verification

`tests/test_session_log.py` 验证有序回放、序号断裂、session 混入、事件生成和过期版本冲突。`tests/test_session_runtime.py` 继续验证 AgentSession 能恢复 replay 后的历史，并保留失败或取消前已经提交的用户消息。`examples/05_session_replay.py` 展示从事件日志重建快照。
