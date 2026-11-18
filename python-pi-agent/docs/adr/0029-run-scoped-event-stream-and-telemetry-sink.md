# ADR 0029: Run 级事件流与 TelemetrySink 端口

> 状态：已接受（Accepted）
> 关联：[路线图](../roadmap.md) C1、C2、C3；ADR 0013、0015、0021、0024

## 背景

ADR 0021 已经提供版本化 `AgentEventEnvelope`（session/run/turn、request/attempt、span、duration、`durable_event_id`），ADR 0013 提供隔离、脱敏与有界缓冲。**生产侧**已经足够，**消费侧**仍有三处缺口：

1. **无 run 级订阅。** `RuntimeSession.run()` 只返回最终 `AgentResult`。Web/SSE 应用要把一次 run 的 text delta 与工具进度推给对应客户端，只能挂一个全局 `EventSink`，再自行按 session/run 过滤和路由；`follow_up()` 只返回 run ID，调用方拿不到该 run 的事件。
2. **无遥测导出。** AGENTS.md §6 列出的 `TelemetrySink` 端口不存在；没有 OpenTelemetry 映射，没有低基数指标（请求延迟、token、工具失败率、retry/fallback 次数），也没有标准库 `logging` 适配。
3. **无观察侧用量累计。** 已有 `model_usage` 和 `AgentResult.usage`，但观察端没有完成事件中的 run 合计、跨 run 的 Session 汇总或成本估算。

## 决定

### 1. `EventHub`：Runtime 拥有的观察扇出

- `AgentRuntime` 持有一个 `EventHub`；Runtime、Session、插件与模型组合层通过同一个隔离入口发布。Hub 与已有应用 sink 独立扇出，后者保留同步顺序和错误隔离；
- `hub.subscribe(filter: EventFilter, *, max_buffer: int, overflow: Literal["drop_oldest", "drop_newest", "close"]) -> EventSubscription`；
- `EventSubscription` 是 `AsyncIterator[AgentEvent]` + `aclose()`，每个订阅者使用独立的有界队列；**发布永不 await 订阅者**，满队列按 overflow 策略处理并计入 `dropped_count`；
- `EventFilter` 支持 session/run/kind/channel。放弃消费须显式关闭；Runtime 关闭时解除订阅，允许已缓存的事件排空。

### 2. `RunHandle`：调用方持有的一次 run

```python
handle = await session.submit(prompt)          # 入队即返回，不等待完成
async for event in handle.events():            # 只含本 run_id 的事件，settled 后结束
    ...
result = await handle.result()                 # 与现有 run() 结果一致
await handle.abort()
```

- `RuntimeSession.run(prompt)` 保留；等待 `run()` 的任务被取消时仍会取消相应 run。等待 `handle.result()` 的任务被取消时不会取消 run；
- `RuntimeSession.follow_up()` 保存 durable control 后返回 `RunHandle`；ID 通过 `handle.run_id` 取得。过渡期提供 `follow_up_id()`，底层 `AgentSession.follow_up()` 保持原签名；
- 订阅在 run 入队**之前**建立，`run_started` 在开始分派时发送，涵盖 Session 准备和首次自动压缩。分派前被取消的 run 直接发送取消和 settled 终态；
- 句柄持有一个有界、仅消费一次的流，默认缓存 256 条，丢弃新事件并额外保留 `run_settled`。重连使用 durable log（`read_events`）恢复已提交事实，已消费或丢弃的 live 事件不重放。

### 3. `TelemetrySink` 端口

```python
class TelemetrySink(Protocol):
    def on_event(self, event: TelemetryEvent) -> None: ...  # 必须快速返回
    async def aclose(self) -> None: ...
```

- 与 `EventSink` 分开：`EventSink` 面向业务/UI，可接收脱敏后的 payload；`TelemetrySink` 只消费 envelope 与低基数字段，**不接收任何 payload 正文**，从类型上杜绝 prompt/工具参数进入指标系统；
- 内置实现：
  - `LoggingTelemetrySink`：结构化输出到标准库 `logging`（logger 名 `pi_agent_py.telemetry`），不配置 handler；
  - `OtelTelemetrySink`（optional extra `pi-agent-py[otel]`）：run → turn → model_request/tool/compaction/hook，provider attempt 嵌套在模型请求下。指标覆盖模型延迟、token、工具执行、provider attempt、retry 和 fallback；tool/provider 名称采用显式 allowlist，其余归为 `other`；
- 未闭合的 started span 在 run settled 时以 `cancelled`/`error` 状态强制结束，避免 exporter 中出现悬挂 span。
- Builder 按 owned/borrowed 管理 sink；应用负责日志 handler 与 SDK 生命周期，慢 I/O 使用日志队列或批量 span exporter。

### 4. `UsageLedger`

- 按 run 与 Session 累计 `model_usage`；每个逻辑模型请求采用最后一份用量报告，失败/取消前已报告的用量也纳入；
- `run_completed` payload 增加该 run 累计 usage，`AgentResult.usage` 语义保持不变。Session 汇总保留至 Runtime 生命周期结束，已结束的 run 默认保留最近 256 条；
- `PricingTable` 注入按模型的美元单价（每百万 token），缺价通过 `unpriced_requests` 标识。成本不作为计费依据。账本不持久化，也不会猜测没有报告的用量。

## 备选方案

1. **只提供 `CallbackEventSink`，由应用自行过滤**：拒绝。每个下游都会重写一遍路由与背压，且容易在回调里阻塞主循环。
2. **`AgentSession.run()` 直接改为 async generator**：拒绝。会把结果获取与事件消费耦合在一起，调用方不迭代就拿不到结果，并破坏 FIFO dispatcher 的语义。
3. **把 OpenTelemetry 作为核心依赖**：拒绝。核心保持只依赖 httpx/PyYAML；OTel 作为 extra。
4. **把遥测并入 `EventSink`**：拒绝。会让指标系统有机会接触 payload 正文，违反数据最小化。
5. **live 事件持久化以支持断线重放**：拒绝。text delta 体量大且非事实；恢复走 durable log。

## 后果

- Web/SSE 参考应用可以直接 `async for event in handle.events()`，不再依赖全局 sink；
- 慢订阅者只会丢弃自己的事件，不影响 loop、持久化或其他订阅者；
- 运维侧可以从 OTel 看到 run → turn → model/tool 时间线和指标，而 prompt 与工具参数不会进入遥测；
- `RuntimeSession.follow_up()` 返回类型变化已记录在 CHANGELOG，过渡期提供 ID-only 方法；
- `EventHub` 增加一个后台无关的纯内存组件，Runtime 关闭顺序需要在 `runtime_closed` 之后、observer 关闭之前关闭订阅。

## 验证方式

- FakeProvider 驱动：两个并发 Session 的 `RunHandle.events()` 只包含各自 run_id；`run_started` 一定是首个事件，`run_settled` 后迭代器结束；
- 背压：订阅者永不消费时，loop 照常完成，`dropped_count` 正确，其他订阅者事件完整；
- 取消：`handle.abort()` 后事件流以 `run_cancelled` → `run_settled` 结束；订阅者在迭代中途 `aclose()` 不影响 run；
- `TelemetrySink` 测试断言收到的数据中不存在 message/text/arguments 字段；
- OTel 测试使用 in-memory span exporter 校验 span 父子关系、duration 与未闭合 span 的强制结束；
- 通过给定事件时间戳验证 elapsed duration，不依赖真实等待。运行示例和练习见[第 29 章](../../tutorials/29-run-observation.md)。
