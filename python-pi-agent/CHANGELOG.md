# 更新记录

## Unreleased

- 新增 SessionOpenRequest、SessionSpec 和 SessionSpecResolver，支持每次打开时解析完整组件和观察 metadata。Builder 支持实例注册与基于默认组件的 resolver factory，同 ID 的冲突规格会被拒绝。metadata 作为 envelope 的 JSON 字段序列化并参与日志脱敏。

- `RuntimeSession.submit()` 和 `RuntimeSession.follow_up()` 返回 `RunHandle`，提供 run 事件流、独立结果等待和定向取消。原先使用 `follow_up()` 返回字符串的调用改用句柄的 `run_id`；过渡期可使用 `follow_up_id()`。底层 `AgentSession.follow_up()` 的签名不变。
- 新增 Runtime 事件订阅、结构化日志及可选 OpenTelemetry 遥测适配器，以及 run/Session 用量汇总和可注入的估算价格表。
