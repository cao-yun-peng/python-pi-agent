# ADR 0015: Durable Event Correlation Metadata

## 背景

原有 durable event 只有 `session_id + sequence`。它可以重放消息，却不能回答某条消息属于哪次 run、哪次模型请求，或者是用户、模型、工具、恢复逻辑还是压缩器产生的。并行工具、队列和多 Agent 都需要稳定的因果关联。

## 决定

- 每个 `DurableSessionEvent` 增加稳定 `event_id`、可选 `run_id`、可选 `turn_id` 和强类型 `EventProvenance`。
- `event_id` 使用确定性的 `<session_id>:<sequence>`，不依赖随机数。
- 一次 `AgentSession.run()` 使用一个 `run_id`；每次模型请求使用 `<run_id>:turn:<request_number>`。
- provenance 记录 source、actor、cause 和 fork 来源标识，不复制消息正文或密钥。
- JSONL 写入 schema v4；v1-v3 读取时生成稳定 event ID，并标记 `source="legacy"`。
- fork 为目标 session 生成新的 event ID，并用 `copied_from_event_id` 指回源事件。

## 备选方案

- 只把 ID 放进日志 payload：缺少领域校验，Store 和 runtime 容易产生不同格式。
- event ID 使用 UUID：全局唯一，但纯 transition 测试不可预测，且当前单 session 序号已经足够建立稳定身份。
- 强制旧日志离线迁移：增加教学和部署门槛，恢复前没有必要。

## 后果

Store append/compact 接收 `SessionEventContext`；runtime 必须在每次提交时声明 provenance。JSONL schema 增加一版，但旧日志保持可读。队列本身仍是进程内控制状态，只有真正消费并提交的 steer/follow-up 才成为 durable message。

## 验证

- transition 测试验证稳定 event ID 和 run/turn/provenance。
- JSONL 测试验证 schema v4 round-trip 和旧 schema 兼容。
- fork 测试验证目标 event ID 与源事件链接。
- session 控制测试验证 steer 消息继承当前 run，并进入下一 turn。
