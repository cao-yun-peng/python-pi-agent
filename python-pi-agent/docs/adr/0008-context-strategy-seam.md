# ADR 0008: Separate Durable History from Model Context Projection

## Status

Accepted

## Context

Agent 之前把全部 committed messages 直接传给 `ModelProvider`。这能保持实现简单，但未来加入 token budget、裁剪或压缩时，如果直接修改 `Agent.messages`，会破坏 durable log 的可回放事实；如果把策略写进 runtime，又会让主循环绑定具体算法。

## Decision

- `ContextPlan` 是一次模型请求的显式上下文结果，记录选中消息、源消息数量、省略数量及可选预算信息。
- `ContextStrategy` 是异步端口，以完整 committed history 为输入，不得修改它。
- `FullHistoryContextStrategy` 是默认实现，保持现有模型可见行为不变。
- `Agent` 在每次 Provider 请求前生成新计划；`AgentSession` 只负责把显式注入的策略传给 Agent。
- 成功准备后发出不含消息正文的 `context_prepared` capability event。
- 普通策略异常转换成 `ContextPreparationError`；取消不包装并继续传播。

## Alternatives

- 在 `Agent` 中直接写裁剪算法：会让循环承担策略职责，并增加替换成本。
- 裁剪 `Agent.messages` 或 SessionStore 日志：会永久丢失事实，使 replay 与 fork 得到不同结果。
- 让每个 ModelProvider 自己裁剪：多个 Provider 会产生不一致策略，也无法在供应商调用前统一观察。
- 使用同步端口：当前默认实现更简单，但会阻碍后续可取消的资源加载和压缩调用。

## Consequences

- durable history 与单次模型视图有明确边界。
- 每轮工具续写都会重新准备上下文，因此策略可看到刚提交的 tool result。
- 默认策略没有 token 成本优化；预算、消息组和摘要规则仍需后续实现。
- 策略是 run 延迟的一部分，未来涉及 I/O 的实现必须定义超时和资源生命周期。

## Verification

- FakeProvider 测试证明 Provider 接收投影，而 Agent 和 SessionStore 保留完整历史。
- 事件测试证明 `context_prepared` 暴露数量且先于模型请求。
- 失败测试证明策略异常使用独立错误类别，且不会调用 Provider。
- 取消测试证明取消穿过异步策略并最终进入 settled。
- `examples/08_context_strategy.py` 展示默认策略的一次可观察执行轨迹。
