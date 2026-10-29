# 第 24 章：Provider Retry、Backoff、Router 与 Fallback

## 1. 要解决的问题

一次 Agent turn 是一个逻辑模型请求，但真实网络调用可能经历限流、超时、连接失败和供应商故障。若把 retry 直接写入 `Agent.run()`，主循环会同时承担协议消费、供应商选择、等待策略和错误分类，难以替换和测试。

本章在原始 `ModelProvider` 上增加两个可替换组合层：`DirectModelRuntime` 始终只调用一次并保留原始错误，`ResilientModelRuntime` 则在内部选择显式注册的 `ProviderCandidate`，为每次物理调用生成独立 attempt identity，并执行 retry、backoff 或 fallback。Agent 对两者仍然只发起一次逻辑请求。

```text
Agent logical request
  -> ResilientModelRuntime
       -> RoutePolicy
       -> Provider attempt 1
       -> Backoff + Provider attempt 2
       -> compatible fallback attempt 3
```

## 2. 最小可运行示例

运行：

```powershell
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
python .\python-pi-agent\examples\23_provider_resilience.py
```

示例只使用 `FakeProvider`。Primary 连续超时两次，随后 runtime 选择 fallback。它不访问网络，也不会产生模型费用。

## 3. Retry 的安全边界

默认 `DefaultRetryPolicy` 只重试明确分类为 transient 的错误：rate limit、timeout、transport 和标记为 retryable 的 HTTP 错误。认证、余额、配置和协议错误立即失败。

更重要的边界是：只要当前 attempt 已向上游交付任意流事件，就不再透明 retry。否则 UI 可能先看到半段文本，再看到第二次 attempt 的完整文本；ProviderState、usage 或 tool call 也可能被错误拼接。失败 attempt 的 assistant message 不进入 durable history，但已经发送的 live delta 不能假装从未发生。

## 4. Backoff 与取消

`FullJitterBackoff` 使用指数上界和 full jitter；429 的数字形式 `Retry-After` 优先于本地公式，并受 `max_delay_seconds` 限制。sleep、clock 和随机源均可注入，因此测试不依赖真实等待。

取消在 Provider stream 或 backoff sleep 中都原样传播。取消不会启动下一 attempt，最终仍由 Agent 发出一次 `run_settled`。

## 5. Router 与 Provider-owned State

`CapabilityRoutePolicy` 按以下条件过滤候选：

- request purpose 是否为 `agent_turn` 或 `compaction`；
- 有工具 schema 时候选是否支持工具；
- 最近 assistant message 的 opaque `ProviderState` owner 是否被候选声明接受。

最后一条防止把 TiMi Responses 的 encrypted reasoning/output item 静默交给不兼容 Provider。没有兼容候选时显式抛出 `ProviderRoutingError`；runtime 不丢弃 continuity state。

## 6. 可观察事件

逻辑请求仍由 Agent 发出 `model_request_started/finished/failed`。runtime 额外发出 capability events：

```text
provider_route_selected
provider_attempt_started
provider_attempt_failed
provider_retry_scheduled
provider_fallback_selected
provider_attempt_succeeded
```

同一逻辑请求共享 `request_id`，每个物理调用拥有独立 `attempt_id`。事件只记录候选名、attempt、错误类型、retryable、是否已开始输出、delay 和 duration，不保存 prompt、密钥或原始响应正文。

## 7. 配置与显式组装

`agent.yaml` 可保存非秘密 retry/backoff 上限：

```yaml
resilience:
  enabled: true
  max_attempts_per_provider: 2
  max_total_attempts: 4
  max_elapsed_seconds: 30
  base_delay_seconds: 0.25
  max_delay_seconds: 8
```

Fallback 候选仍由应用显式组装。这样每个 Provider 的 credential、capability 和 state owner 都可审查，不依赖 import 副作用或全局 registry。

## 8. 失败、测试与主动省略

全部 retryable 候选耗尽后抛出 `ProviderExhaustedError`。其中只保存 secret-free `AttemptSummary`，最后一个真实错误保留为异常链 cause。

离线测试覆盖：retry identity、partial output fail-closed、fallback、认证错误、tool capability、opaque state 粘滞、Retry-After、总预算、取消和 settled 语义。

当前主动省略 circuit breaker、动态健康评分、成本/延迟加权、跨模型 state 转换和 partial stream reset。它们应建立在生产 trace 与 eval 数据上，而不是先进入主循环。

## 9. 与 Pi 的映射

Pi 的模型 runtime/registry 负责选择模型和 Provider；本项目把第一次生产可靠性切片收敛成显式 `ProviderCandidate + RoutePolicy + RetryPolicy + BackoffStrategy`。两者都不要求 Agent loop 理解供应商 HTTP 细节。本项目额外坚持：retry/fallback 的每次物理调用必须有独立 attempt trace，opaque provider state 不兼容时 fail closed。
