# ADR 0021: 统一 Observer Event Envelope 与 Span 终止事件

## 背景

`AgentEvent` 原先只有 `kind`、`channel` 和开放的 `payload`。session、run、turn、request 等公共关联字段由不同 emitter 自行塞进 payload，导致字段位置不稳定；部分 AgentSession 与 Plugin 事件没有关联信息。模型请求只有 started，没有 finished/failed，工具、压缩和 Hook 也没有统一耗时。`message_committed` 通知不能指回真正写入 SessionStore 的 durable event。

## 决定

- 为每个 `AgentEvent` 增加 `AgentEventEnvelope`。它固定保存 schema version、事件 ID、wall-clock 时间、source、session/run/turn、request/attempt、span、duration 和可选 durable event ID。
- 公共关联字段不再复制进 payload；payload 只保存事件特有数据。
- `make_agent_event()` 是 runtime、session 和 plugin 的统一构造入口；`serialize_agent_event()` 输出固定 JSON record。
- 模型请求新增 `model_request_finished` 与 `model_request_failed`。started 与 terminal event 共享 span ID，失败和取消都产生 terminal event。
- 工具执行、compaction 和 plugin Hook 的 started/terminal event 共享 span ID；terminal envelope 保存 monotonic duration。
- `NextTurnHook` 接收强类型 `NextTurnContext`，让 run-scoped Hook 事件具有明确 session/run/turn，而不是只知道请求序号。
- `_SessionWriter.commit()` 与 `deliver_control()` 返回 `DurableEventIdentity`，使 `message_committed.envelope.durable_event_id` 能连接 durable log。
- `EventSink` 仍是 best-effort observer。Envelope、span 和 durable link 不把它提升为恢复或审计事实源。

## 备选方案

- 继续把公共字段放在 payload：兼容性最好，但无法建立稳定 schema，也会继续产生 emitter 漂移。
- 直接引入 OpenTelemetry SDK：可以得到标准 exporter，但会掩盖本阶段要学习的 envelope、span 配对与 durable/live 边界。
- 把全部 observer event 写入 SessionStore：可避免丢失，却会把高频 delta、运行指标和模型可见事实混成同一日志。
- 让 message committer 返回完整 durable event：信息更丰富，但当前 Store 端口只返回 snapshot；最小 identity receipt 已足够建立 join。

## 后果

- EventSink 消费者必须从 `event.envelope` 读取公共关联字段。
- Event payload 契约更窄；旧代码若读取 `payload["run_id"]` 需要迁移。
- `NextTurnHook.prepare()` 的调用方与实现方需要从 `next_request_number=` 迁移到 `context=NextTurnContext(...)`。
- 单个 span 可计算模型、工具、压缩和 Hook 耗时，但当前还没有指标聚合、采样或分布式 exporter。
- Standalone Agent 没有 SessionStore，因此其 `message_committed` 可以没有 durable event ID；AgentSession 提交必须有。
- `occurred_at` 用于时间线展示，duration 使用 monotonic clock 计算，避免 wall-clock 跳变影响耗时。

## 验证

- envelope 校验缺失的 run/turn、request/attempt 关系以及负 duration。
- JSON serializer 保持固定字段形状，redaction 保留原 event identity。
- 模型成功、失败和取消均有 terminal event，且与 started 共享 span。
- 工具、compaction 和 plugin Hook terminal event 带非负 duration。
- AgentSession 的 `message_committed.durable_event_id` 与 SessionStore 中实际 event ID 一致。
