# ADR 0017: Durable Session Controls

> 2026-11-22 修订：下文关于继续读取旧版 JSONL 的决定已由 [ADR 0005](0005-jsonl-session-store.md) 的修订取代。当前仅接受 schema v6，控制事件的持久化与恢复语义不变。

## 背景

ADR 0016 建立了 steer、follow-up、abort、settled 和 idle 控制面，但队列仍是进程内 `deque`。调用方收到 follow-up run ID 或 steer 成功返回后，进程退出仍会丢失尚未消费的意图；单独持久化消息与消费标记还会产生重复投递窗口。

## 决定

- steer 与 follow-up 使用 `control_enqueued -> control_claimed -> control_delivered | control_cancelled` durable transition。
- `control_delivered` 同时携带 user message；replay 在一个事实中提交消息并移除 pending control。
- claim 包含 owner、claim ID 和过期时间。其他 dispatcher 在 lease 有效期内不得消费。
- steer 永远绑定接受它的 run。恢复时不存在活动目标 run 的 steer 被持久取消，不得泄漏到后续 run。
- follow-up 入队时预留 run ID；未 claim 或 claim 已过期的 follow-up 可在 `AgentSession.restore()` 时恢复 FIFO 调度。
- abort 清理队列前先持久化 cancellation。
- JSONL schema 升级为 v5，并继续读取 v1-v4。

## 备选方案

- 序列化内存 deque：无法表达 claimed、delivered 和 cancelled，也不能审计恢复决定。
- 分别写 `message_committed` 与 `control_consumed`：两次 append 之间崩溃会导致重复消息。
- 恢复 steer 到下一个 run：可能把针对旧任务的指令应用到无关任务。

## 后果

Session version 现在包含消息、压缩和控制事实，不能再按消息数量理解。运行中的 writer 在提交前刷新 snapshot，以容忍并发控制事件推进版本。已经 delivered 的 follow-up 不会自动重跑；外部工具副作用仍不承诺 exactly-once。JSONL Provider 仍只有实例内锁，多进程文件锁属于后续工作。

## 验证

- replay 对 control enqueue/claim/delivery 做严格状态校验。
- follow-up 在活动 run 后按 FIFO 交付并留下三段 durable 证据。
- 新 AgentSession 能恢复尚未交付的 follow-up。
- 重启后孤立 steer 被 durable cancel。
- abort 对未开始 follow-up 写入 cancellation。
