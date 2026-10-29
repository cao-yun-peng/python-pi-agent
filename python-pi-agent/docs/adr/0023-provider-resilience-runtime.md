# ADR 0023: Provider resilience 作为显式组合层

## 背景

Agent 已为每次 Provider 调用携带 request/attempt/purpose/session/run/turn identity，各 Provider 也能分类认证、余额、限流、超时、HTTP、响应与协议错误，但 runtime 仍只执行 `attempt=1`。直接在 `Agent._request_model()` 中增加 retry 会让主循环承担供应商选择、等待和 fallback state 兼容性，并使 compaction 无法复用同一策略。

## 决定

新增结构化实现 `ResilientModelRuntime`，它继续满足 `ModelProvider` 协议，因此 Agent 与 compaction consumer 无需依赖具体实现。

组合层包含：

- `ProviderCandidate`：显式 Provider、名称、purpose、tool capability 和可接受的 provider-state owners；
- `RoutePolicy`：按请求能力与 continuity state 选择有序候选；
- `RetryPolicy`：根据稳定错误分类、是否已交付输出和 per-provider attempt 决定 retry、next 或 fail；
- `BackoffStrategy`：计算可注入、可取消的等待；
- attempt trace：每次物理调用使用独立 attempt ID，同一逻辑请求共享 request ID。

默认仅在 attempt 尚未向 consumer 交付任何流事件时 retry。认证、余额、配置和协议错误不 retry。Opaque provider state 不兼容时不允许 fallback。

## 备选方案

1. 在 `Agent.run()` 内实现 retry：拒绝，因为污染核心循环，且 compaction 无法复用。
2. 每个 Provider 自己 retry：拒绝，因为 attempt trace、总预算、跨 Provider fallback 和取消语义会分裂。
3. 对 partial stream 自动重试并拼接：拒绝，因为可能重复 UI 文本、usage、tool call 和 provider state。
4. 自动扫描并注册 Provider：拒绝，继续采用显式组装，避免 import 副作用和隐藏凭据来源。

## 后果

- 现有 `Agent(provider)` 调用保持兼容；传入 `ResilientModelRuntime` 即启用组合能力。
- `max_model_requests` 仍统计逻辑 turn，attempt 由独立预算约束。
- retry/fallback trace 是 best-effort capability event，不替代 durable session facts。
- Fallback 可能改变模型行为，因此只有应用显式配置的候选才会参与。
- 当前不实现 circuit breaker、动态加权或跨 ProviderState 转换。

## 验证方式

- 使用脚本化 `FakeProvider` 验证 pre-output retry 与 partial-output fail-closed。
- 使用 fake clock/sleeper 验证 backoff、Retry-After、预算和取消。
- 验证 fallback capability、opaque state 粘滞和 exhausted error 脱敏。
- 验证一次逻辑 run 无论多少 attempts 都只发出一次 `run_settled`。
