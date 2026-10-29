# ADR 0024: Application Runtime 与显式资源所有权

## 背景

`Agent` 已经专注于模型/工具循环，`AgentSession` 已经负责单会话持久化、控制队列和 session-plugin lease，但正式应用仍需逐项创建 Provider、Store、PluginHost、事件管线和 Session。Provider、PluginHost 与 BufferedEventSink 各自拥有异步关闭语义，示例手工关闭容易遗漏，CLI/Web 也无法共享稳定的应用入口。

## 决定

新增位于核心运行时之上的应用层：

- `AgentRuntime` 是长生命周期 facade，拥有可复用的 session controller 池；
- `RuntimeSession` 是引用计数 lease，不向传输层暴露 `AgentSession`；
- `AgentRuntimeBuilder` 是单次使用的事务化组装器；
- `AgentProfile` 只保存代码定义的默认值和零参数 factory，不保存凭据或活 I/O；
- `ProviderCatalog` 由应用显式传入，不使用 import 副作用或进程级全局 registry；
- `ManagedResource` 明确区分 `owned` 与 `borrowed`；
- `LifecycleScope` 在构造失败或关闭时逆序执行全部 owned cleanup；
- observer 使用独立 scope，确保 runtime/component 清理事件发出后才关闭日志或事件 worker。

Builder 从已有 `LoadedAppConfig` 构造原始 Provider，并根据 resilience 配置包裹 `DirectModelRuntime` 或 `ResilientModelRuntime`。原始 Provider 仍由 lifecycle scope 关闭，组合层不夺取其资源所有权。

后续 ADR 0027 将同一显式组装边界扩展到可选 Docker Bash：配置保存非秘密部署事实，`coding_profile()` 保存权限默认值，Builder 才创建活的 Sandbox/Executor/Tool；Profile 仍不持有 I/O。

同一个 session ID 在 Runtime 存活期间共享一个 `AgentSession` controller。容量不足时只能驱逐无 lease、已 restore 且 idle 的 controller；驱逐不会删除 durable history。Runtime 关闭时先拒绝新工作，短暂等待 idle，然后关闭全部 Session、PluginHost/Provider/Store，最后关闭 observer。

为支持静态 plugin Hook 与 durable compaction 共存，`AgentSession` 增加通用 `NextTurnHook` 参数，并按“durable compaction → application/plugin Hook”的顺序组合；Agent loop 本身保持不感知 Builder、Profile 或 PluginHost。

## 备选方案

1. 扩充 `AgentSession` 使其创建并关闭所有资源：拒绝，会让会话控制面成为服务定位器，并模糊 borrowed 资源所有权。
2. 使用全局 Provider/Profile registry：拒绝，会引入 import 顺序、副作用和测试间状态泄漏。
3. 让每个 CLI/Web handler 临时创建 `AgentSession`：拒绝，会破坏 steer/follow-up 的活动 controller 语义并重复打开资源。
4. 要求所有 Port 继承统一 `Closable`：拒绝，纯策略和值对象不应承担虚假生命周期；关闭能力由组装边界显式描述。

## 后果

- `Agent` 与 `AgentSession` 仍可被教学示例和高级调用者直接使用。
- 正式应用获得一个稳定 facade，但 Builder/Profile API 在进入正式 CLI/Web 前仍需通过一轮使用验证。
- 第一阶段 SessionPool 使用惰性 TTL/LRU 驱逐，不启动后台清理任务。
- 第一阶段不提供多租户 `SessionRef`、RunHandle、EventHub 或 Web API；这些能力建立在本 ADR 的 facade 和所有权语义之上。
- Profile factory 创建的对象默认是 runtime-scoped；需要 per-session 状态时继续使用 `SessionPluginFactory`。

## 验证方式

- FakeProvider 验证同 session controller 复用、durable history 和配置驱动 resilience；
- Fake closer 验证构造失败回滚、owned/borrowed、逆序释放、错误聚合和幂等关闭；
- 容量测试验证 leased session 不可驱逐、idle session 可以驱逐；
- 取消测试验证零 grace shutdown 会取消活动 Provider 并只产生一次 `run_settled`；
- plugin 测试验证静态 NextTurnHook 能进入 AgentSession，且 plugin cleanup 先于 Provider、observer 最后关闭。
