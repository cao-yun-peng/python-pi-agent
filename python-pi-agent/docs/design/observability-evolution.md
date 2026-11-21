# 可观测层补充设计：从事件可见到诊断可验证

> 状态：建议方案，待实现；第 1–3 节描述已核对的现状，其余为建议契约。
> 日期：2026-11-19；Python 基线：`8b25f917309645a19858837aa817abee87d04da4`。
> 承接 ADR 0013、0021、0029；实施新增契约前另建 ADR，不改写已接受决定。

## 1. 要解决的问题

当前系统已经能订阅运行、看到模型和工具时间线、导出指标并估算用量。下一步要让使用者可靠回答：**这次运行慢在哪里、为什么失败、观察数据是否完整，以及结果能否与持久事实对应。**

继续复用 `AgentEventEnvelope → EventHub / EventSink / TelemetrySink`。先补齐语义、诊断与确定性验证，再接跨请求 trace；暂不建设独立观测平台、指标数据库或完整审计系统。

## 2. 当前能力与证据

路径均相对 `src/pi_agent_py/`，按符号定位，避免行号漂移。

| 能力 | 当前实现 | 已有测试证据与限制 |
|---|---|---|
| 统一关联 | `domain.py::AgentEventEnvelope`、`observability/events.py::make_agent_event` | `test_events.py::EventEnvelopeTests`；已有 event/session/run/turn/request/attempt/span/durable ID，没有 run 内观察序号或外部 trace context |
| 观察异常隔离 | `IsolatedEventSink`、`application/_observation.py::_RuntimeObservation` | `test_events.py::EventIsolationTests`；隔离异常，不能阻止同步 sink 耗时阻塞 |
| 安全日志 | `RedactingEventSink`、`BufferedEventSink` | 脱敏先于入队、队列满、投递失败、关闭测试；需应用显式接入，不是 Hub 自动脱敏 |
| run 消费 | `EventHub`、`EventSubscription`、`runtime/run_handle.py::RunHandle` | `test_run_observation.py` 覆盖隔离、背压、取消和关闭；单 loop、单消费流，不支持 live 重放 |
| 遥测最小化 | `observability/telemetry.py::_project`、`LoggingTelemetrySink` | 无正文 payload，清空自由 metadata；当前未投影 `run_failed.error_category` |
| span / 指标 | `observability/otel.py::OtelTelemetrySink` | `test_otel.py` 覆盖父子关系、middleware 三种终态、给定耗时和强制闭合；可选 SDK 未安装时测试跳过 |
| 用量与成本 | `observability/usage.py::UsageLedger`、`PricingTable` | 最终报告、失败请求、缺价、控制器驱逐测试；Runtime 内累计，不含重启前数据或未报告用量 |

已有指标是 `pi_agent.model.request.duration`（ms）、`pi_agent.tokens`、`pi_agent.tool.executions`、`pi_agent.provider.attempts/retries/fallbacks`。当前没有 run 延迟、排队时长、首文本延迟和观察投递健康指标。cached token 是 input 的子集，展示总量时不能把 input、output、cached 三条序列直接相加。

## 3. 三条数据路径及可信边界

```text
SessionStore append → durable fact → replay → SessionSnapshot
                             ↑ durable_event_id
producer → AgentEvent → Runtime observation
                        ├─ UsageLedger（先记账，不经订阅队列）
                        ├─ EventHub → RunHandle / 独立订阅（原始事件，允许丢失）
                        ├─ TelemetryEvent 投影 → Logging / OTel（无正文）
                        └─ application EventSink（日志需先 Redacting 再 Buffered）
```

- `channel="durable"` 的 observer 通知仍可能丢失。只有 Store 中已提交的记录是恢复事实；通知缺失不能推断提交失败。
- `EventHub` 过滤是路由，不是租户鉴权。应用先授权 Session，再建立过滤订阅；原始文本只提供给有权查看的调用方。
- `event_id` 标识观察记录，`durable_event_id` 连接提交事实，`span_id` 配对阶段，request/attempt 区分逻辑请求与物理尝试。这些 ID 都不是指标标签。
- `occurred_at` 用于时间线；阶段耗时沿用 monotonic duration。OTel 当前 run/turn 起止使用事件 wall clock，因此还没有 run 总耗时抵御时钟跳变的保证。
- 外层 frozen dataclass 不等于 payload 深层不可变；当前 Hub 和应用 sink 共享事件引用。消费者必须只读，后续需用契约测试保护这一边界。

## 4. 第一阶段：补齐事件和错误契约（优先）

### 4.1 生命周期清单

维护一份按事件 kind 的契约表：producer、channel、必需关联字段、payload 白名单、started/terminal 配对、是否允许无 run，以及 durable join 规则。先核对下面九阶段，不为每个函数都创建 span。

| Agent 阶段 | 现有证据 | 建议补齐 |
|---|---|---|
| 接收输入 | 分派开始的 `run_started`，消息提交通知 | 区分 submit 接受时间与 dispatcher 开始时间，得到排队耗时 |
| 组装上下文 | `context_prepared`、compaction / hook / middleware 事件 | 独立上下文耗时与固定原因分类；不记录检索片段 |
| 发起模型请求 | model_request span、provider attempt span | 保持逻辑请求与 attempt 分离；retry/backoff 单独归因 |
| 消费流 | text/thinking delta、usage | 首个非空 text delta 延迟；不把 thinking 或工具调用冒充首文本 |
| 完成校验 | model_request terminal、message_committed | 协议失败与提交失败区分；模型成功不等于消息提交成功 |
| 校验和调度工具 | validation / approval / denied 事件 | 按调用身份检查覆盖，必要时增加审批耗时；不扩张到工具正文 |
| 执行和回填工具 | tool span、middleware span、提交结果 | 保留工具失败被模型处理后 run 仍成功的语义 |
| 继续或结束 | 后续 request、retry/fallback、compaction | 仅在有真实消费需求时增加固定枚举的决策原因 |
| settled | run_completed/failed/cancelled → run_settled | 在正常进程执行中，每个已接受 run 仅一个 settled；排队取消允许没有 started |

### 4.2 错误投影

建议为 `TelemetryEvent` 增加可选强类型 `error_category`。复用 runtime 已有错误分类，经固定枚举映射；未知值归为 `other`。原始异常文本、异常链、堆栈、URL 和工具结果均不进入该字段。不要将分类放进自由 metadata 再让遥测读取。

`run_failed` 的分类应进入结构化日志、run span 属性和低基数失败计数。模型、工具、middleware 的 outcome 保持各自语义：工具错误可被处理并最终成功；取消独立计数；SDK 关闭时强制结束 span 只表示观测清理，不能据此推断业务 run 被取消。

### 4.3 观察副作用隔离

先增加“应用 sink 修改嵌套 payload 不影响 Hub 消费者、其他 observer 或 durable state”的回归测试，再选择在扇出边界复制或递归冻结受支持的事件值。以正式事件 payload 的类型范围为依据，不对任意对象盲目 deepcopy。红线是观察者不能改变执行或其他观察结果。

同步 `EventSink.emit` 和 `TelemetrySink.on_event` 必须快速返回；Runtime 只隔离异常。慢日志走现有 Buffered/QueueHandler，SDK 走应用配置的批量 exporter，不为每个事件创建后台 task。

## 5. 第二阶段：让观察层自身可观测

当前 `dropped_count` 与有界 failure 明细是局部诊断，明细最多保留最近若干条，不能当作累计失败数。

建议提供只读、无正文的健康快照，至少包含 active subscriptions、queued events、累计 dropped、累计 delivery failures、last failure category。按 consumer 类别和 overflow policy 聚合；禁止用 run/session/subscription ID 作指标维度。排队数量只用于诊断，不能作为运行恢复条件。

指标导出失败不能再向同一个失败 sink 递归发送事件。健康计数在本地更新，通过快照读取或独立 metrics 路径导出。插件卸载、订阅关闭、Runtime 关闭均释放引用。

如 UI 需要明确展示缺口，再新增 run 内单调 `sequence`：在 Runtime 的唯一观察入口、过滤和丢弃之前分配；范围明确为当前 Runtime 中的 run，不能替代 durable version。schema 升级保留旧记录缺字段的读取语义；过滤也会形成序号间隙，因此必须结合过滤条件与丢弃数解释，不能仅凭跳号断言丢失。暂不承诺跨 Runtime 的全局顺序。

订阅结束仍沿用单流语义：RunHandle 满队列时额外保留 settled；通用 Hub 订阅没有这一保证。中途 `aclose()` 清空队列，Hub 关闭允许排空。UI 看到 settled 只代表结束，不代表中间过程完整。

## 6. 第三阶段：性能指标和跨请求关联

### 6.1 指标定义先于实现

以下为建议名称，单位和标签进入 ADR 后固定。

| 指标 | 计算边界 | 维度与缺失处理 |
|---|---|---|
| `pi_agent.run.duration`（ms） | dispatcher started → settled，monotonic | outcome；排队取消不伪造执行耗时 |
| `pi_agent.run.queue.duration`（ms） | submit 接受 → dispatcher 开始或排队取消 | started/cancelled；不是模型耗时 |
| `pi_agent.model.first_text.duration`（ms） | model_request_started → 首个非空 text delta | 每个逻辑请求最多一次；纯工具请求不记录样本 |
| `pi_agent.context.duration`（ms） | context strategy 调用起止 | 固定 outcome，不包含消息内容 |
| `pi_agent.observer.dropped` / `.failures` | 本地健康累计增量 | 固定 consumer/stage/reason，无 ID |

模型请求延迟包含内部重试时的等待，物理 attempt 延迟另看 attempt span，不能两者相加。首文本延迟不等于网络首字节延迟。所有新增时间源可注入，以假时钟覆盖 wall-clock 跳变与取消，不用真实 sleep 验证阈值。

### 6.2 外部 trace context

当前 `OtelTelemetrySink._start()` 使用新的 `Context()` 创建根，因此没有自动继承 HTTP 入站 trace，也没有将 OTel trace/span ID 回写 envelope。已有内部 span ID 与 SDK 生成 ID 是两套标识。

建议先做最小关联：在 SDK span 属性写入内部 event span ID，并在应用边界为 run 显式绑定可选 parent trace context。不要从后台 dispatcher 的 ambient ContextVar 猜父 span，也不要把任意 baggage、凭据或请求头写入 metadata。父上下文绑定随 run 结束释放；没有父上下文时维持现有根 trace。

跨进程传播属于后续独立接缝：由 HTTP/MCP 适配器在信任边界解析、验证、注入，domain/ports 不依赖 OTel SDK。不在这一阶段扩大 Provider wire 接口或修改 durable log 来存 SDK 对象。

## 7. 用量、成本和审计边界

1. 保持当前每个逻辑请求最后一次报告的计量语义。`UsageLedger` 本身不按 event/request ID 去重；不能直接把重放事件再次灌入账本并声称幂等。
2. 订阅丢事件不影响前置记账；Provider 不报告用量、独立压缩策略、重启前历史仍可能缺失。`unpriced_requests` 只表示已收到报告但缺价，不能衡量未报告请求数。
3. retry/fallback 的物理请求可能产生费用，而逻辑请求账本未覆盖全部 attempt。先明确 `reported usage`，如需实际成本归因，再独立设计 attempt 级报告、去重键与逻辑结果的关系。
4. 建议将完整性拆为“用量报告覆盖”和“价格覆盖”，价格表增加应用可指定的版本来源；不自动联网查询价格，不把成本估算作为账单。
5. Session 汇总当前一直保留至 Runtime 结束，不随 controller 驱逐清除；长生命周期且 Session 数量很大时需要独立保留策略，不能误认为 recent-run 上限限制了所有内存。
6. 审计必须独立设计持久提交、访问控制与保留策略。observer best-effort、工具日志和 span 都不能证明外部副作用 exactly-once。

## 8. 实施边界与验收

| 批次 | Definition / Provider / Consumer | Lifecycle | 必须新增的离线证据 |
|---|---|---|---|
| O1：契约和分类 | kind 清单、TelemetryEvent 分类；Logging/OTel 消费；保护扇出值 | 保留现有 ownership | Provider/Store/内部失败分类；工具失败后 run 成功；嵌套修改隔离；取消 settled 唯一性 |
| O2：投递健康 | 强类型快照、本地计数；Hub/Buffered 更新，应用读取 | 关闭释放订阅，不递归报错 | 慢/不消费、三种 overflow、计数超过明细上限、emit-after-close、独立订阅不受影响 |
| O3：耗时与关联 | 注入时间源、固定指标、run parent binding；可选 OTel 实现 | 清理 span 与绑定，应用 flush/shutdown SDK | 假时钟、零 text、retry/fallback、排队取消、无父/有父 trace、跨 Session 不串联 |

每批保持最小纵向切片，更新示例 30、第 13/29 章、知识地图与对应 ADR。新增公共值对象再经 `api.py` 导出并补契约测试。无需新增 AgentComponents 中的遥测字段；观测仍由 Runtime 基础设施拥有。

验证顺序：`test_events` → `test_run_observation` → `test_otel` → 受影响的 Session/Provider 测试 → Ruff/mypy。OTel 验证环境安装 extra 并确认没有跳过；默认路径仍离线、无 exporter 网络流量。在 Windows restricted sandbox 中，异步和 Temp 测试直接按 AGENTS.md 的沙箱外规则运行。

上游对照沿用 [upstream-mapping.md](../upstream-mapping.md) 固定 Pi commit 下的 lifecycle event / telemetry context；本文的新健康快照、指标和 parent binding 是 Python 演进建议，尚未实现，不宣称与上游 API 兼容。
