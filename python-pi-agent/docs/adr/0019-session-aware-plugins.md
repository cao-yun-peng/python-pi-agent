# ADR 0019: Session-aware Plugin Leases

## 背景

PluginHost 原先只发布 Tool 和 NextTurnHook 静态快照。插件不知道当前服务哪个 Session，也没有按 Session 创建和释放状态的生命周期。直接把 AgentSession、SessionStore 或 messages 暴露给插件会允许其绕过 durable transition，并在并发 Session 间泄漏状态。

## 决定

- PluginAPI 增加显式 `register_session_plugin()`，由 PluginHost 为每个 Session 调用 factory。
- factory 只接收不可变 `SessionPluginContext(session_id)`，返回独立 SessionPlugin 实例。
- `PluginSessionLease` 固定 factory 顺序和实例集合；Provider request Hook 只接收 `ProviderRequestContext + ContextPlan`，返回新的 ContextPlan。
- Hook 不得改变 `source_message_count`，也拿不到 SessionStore 或 canonical message list。
- request interceptor 按 priority/registration order 串行、可配置超时、失败关闭并传播取消。
- `on_run_settled` 是后台 best-effort observer；错误进入隔离 EventSink，不改变 run 结果。lease close 等待观察任务后逆序关闭插件实例。
- 有活动 lease 时拒绝单独卸载插件；PluginHost.close 先关闭 lease，再卸载全局插件。
- AgentSession 增加幂等 async close 和异步上下文管理。

## 备选方案

- 插件维护全局 `dict[session_id, state]`：清理、并发和泄漏责任不清晰。
- 将可变 history 交给插件：内存上下文可能与 replay 分叉。
- 在 host unload 时强制撤销运行中实例：会使同一 run 的 Hook 集合中途变化。
- await settled observer：慢 I/O 会阻塞 dispatcher 和后续 follow-up。

## 后果

session-aware 插件当前只拥有请求投影、settled 观察和资源生命周期，没有直接 steer/follow-up 或 durable 写入权限。若以后需要插件发控制消息，应注入复用 ADR 0017 的受限控制端口，而不是暴露 AgentSession 私有状态。

## 验证

- 两个 Session 使用独立插件实例和不同 session ID。
- 插件注入只出现在 Provider 请求，SessionStore 历史不变。
- Hook timeout 在 Provider 调用前失败关闭。
- abort 后 close 只释放一次插件资源。
- 活动 lease 阻止 unload；host close 先释放 lease。
