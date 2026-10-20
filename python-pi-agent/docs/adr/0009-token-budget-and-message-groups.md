# ADR 0009: Budget Context by Complete Message Groups

## Status

Accepted

## Context

`ContextStrategy` 已能为单次模型请求生成投影，但默认策略仍发送全部历史。简单按单条消息从尾部截取会把 assistant tool call 和对应 tool result 分开，产生协议无效或语义残缺的上下文。Token 计算也随模型和供应商变化，不应固定在选择算法里。

## Decision

- 新增同步 `TokenEstimator` 端口；实现必须返回确定性的非负整数。
- `CharacterTokenEstimator` 提供无第三方依赖的 chars-per-token 近似实现，并计入 tool call、thinking 和 tool identity；只供程序使用的 structured content 不计入模型预算。
- `group_context_messages()` 把每条 system 消息作为 pinned group，把一个 user 到下一个 user 前的所有消息作为不可拆分 turn。
- `TokenBudgetContextStrategy` 固定保留 system groups 和最新 turn，再从新到旧加入完整 turn；第一个不满足预算的 turn 会终止选择。
- mandatory context 超预算时抛出带 required/budget 数据的 `ContextBudgetExceededError`，不做隐式截断。
- 策略只生成 `ContextPlan`；不修改 Agent messages、SessionStore 或 durable event log。

## Alternatives

- 逐消息倒序选择：实现更短，但可能留下孤立 tool result 或未完成 tool call。
- 跳过过大的近期 turn 后继续选择更早 turn：利用率更高，但会制造非连续历史和时间线空洞。
- 在策略中绑定某个供应商 tokenizer：更精确，但破坏 Provider 中立性和离线测试能力。
- mandatory context 超预算时静默截断：运行可能继续，但 system 指令、用户输入或工具协议会在没有证据的情况下改变。

## Consequences

- Provider 始终收到完整的近期 turn，工具关联不会被预算策略拆开。
- 字符估算只能提供安全决策线索，不保证等于供应商计费 token。
- 当前预算只覆盖消息，不包含工具 definition、供应商包装和输出 token reserve。
- 长 system prompt 或超长当前 turn 会明确失败；后续 compaction/输入策略需要处理该失败。
- 整 turn 粒度比 Pi 的切点算法更保守，可能少利用一部分可用窗口。

## Verification

- 分组测试覆盖 user、assistant tool call、tool result、最终 assistant 和下一个 user 的边界。
- 预算测试覆盖连续近期后缀、工具 turn 原子性和 mandatory context 超限。
- estimator 测试覆盖确定性计数和非法返回值。
- Agent + FakeProvider 测试证明 Provider 收到预算投影，而完整历史仍被提交。
- 示例展示 `context_prepared` 中的预算、估算和省略数量。
