# ADR 0013: 隔离并脱敏事件观察链路

## 背景

runtime、session 和 plugin host 原先直接同步调用应用传入的 `EventSink`。观察者异常可能终止 Agent 或覆盖原始错误；同时事件包含 prompt、thinking、工具参数和结构化结果，直接记录会泄露敏感数据。慢日志 I/O 还会增加模型循环延迟。

## 决定

1. `Agent`、`AgentSession` 和 `PluginHost` 自动用 `IsolatedEventSink` 包装观察端口。观察者普通异常不进入控制流，只形成有界、无异常消息的 `EventSinkFailure` 诊断。
2. 脱敏不修改领域事件；日志端显式组合 `RedactingEventSink`，默认隐藏模型可见正文、thinking、工具参数和结构化结果，并递归处理常见敏感键。
3. 慢 I/O 使用可选 `BufferedEventSink`。它采用有界队列、顺序后台 worker、非阻塞 enqueue、显式 `flush()` / `aclose()` 生命周期。
4. 队列满或日志失败允许丢失观察事件。任何恢复所需事实必须进入 `SessionStore` durable log，不得依赖 EventSink。
5. `NextTurnHook` 等拦截器不使用此策略，继续 awaited、超时受控并 fail-closed。

## 备选方案

- 在每个 `_emit()` 调用点单独 `try/except`：重复且容易漏掉 session/plugin 路径，也难以统一诊断。
- 默认把所有 sink 异步化：会改变现有 UI/测试的即时事件语义，并引入所有应用都必须管理的 worker。
- 在 runtime 生成时直接删除敏感字段：会破坏 UI、调试器和测试对原始事件的合法需求。
- 让日志失败终止 run：适用于审计型强一致写入，但这类事实应建模为 durable capability，而不是观察型 EventSink。

## 后果

- Agent 结果与观察基础设施解耦，原始异常不再被 settled 日志失败覆盖。
- 日志应用必须显式采用脱敏适配器；原始 EventSink 仍可能看到敏感数据。
- 缓冲通道是 best-effort，必须查看 `failures` 并在关闭时调用 `aclose()`。
- 诊断只保留异常类型，排障细节减少，但避免二次泄密。

## 验证

- sink 在 run 开始、失败和 settled 阶段抛错时，成功结果或 Provider 原始异常保持不变。
- AgentSession 与 PluginHost 使用相同隔离边界。
- 脱敏后的事件隐藏正文、thinking、工具参数、结构化结果和敏感键，原事件保持不变。
- 后台 sink 顺序投递，下游失败与关闭后 emit 只形成诊断。
