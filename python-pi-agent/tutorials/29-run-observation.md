# 第 29 章：订阅一次运行、导出遥测与累计用量

一次 run 有三种不同的输出：调用方等待的 `AgentResult`、UI 消费的过程事件，以及用于排障的时间线和用量。UI 可能很慢或断开，结果与持久化仍由原来的 FIFO 调度器和 SessionStore 负责。

安装项目后，在 `python-pi-agent` 目录运行 `python examples/30_run_observation.py`。它使用 FakeProvider，不读取凭据或访问网络。从输出中找出两次运行各自的 `run_started → model_request_started → model_usage → run_completed → run_settled`，以及第三次运行的 `run_cancelled → run_settled`。

## 先订阅，再入队

`RuntimeSession.submit(prompt)` 返回 `RunHandle`。Session 先产生 run ID 并绑定订阅，再交给 FIFO dispatcher，所以成功开始分派的 run 以 `run_started` 为首。调用方可以独立等待 `handle.result()`，或者使用 `async with handle.events() as events` 迭代事件。等待结果的协程被取消或订阅被关闭时，已提交的 run 仍可完成；`handle.abort()` 则只取消句柄对应的 run，取消发生在入队后、分派前时没有 `run_started`。便捷接口 `await session.run(prompt)` 仍会在等待者取消时取消对应运行。

`handle.events()` 每次返回同一个迭代器。尚未读取的事件可以在结束后排空，已消费或丢弃的事件不能重放。默认缓存 256 条，满时丢弃新事件，并额外保留一个 `run_settled` 位置。只需某一类事件时，可以从 `runtime.event_hub.subscribe(EventFilter(...))` 创建独立订阅，按 session/run/kind/channel 过滤，并选择 `drop_oldest`、`drop_newest` 或 `close`。通用订阅由调用方或 Runtime 关闭。丢弃数可从各订阅的 `dropped_count` 读取。断线恢复使用 `SessionStore.read_events()` 找回已提交事实。

应用侧 `RuntimeSession.follow_up()` 仍先保存 durable control，再返回句柄；原来直接使用返回 ID 的调用改成 `(await session.follow_up(prompt)).run_id`。底层 `AgentSession.follow_up()` 仍返回 ID。

## 从事件投影出遥测

Runtime 在投递事件时构造 `TelemetryEvent`。该值对象只有 envelope、事件分类、受限的 provider/tool 名称、结果分类和 token 计数，没有正文 payload。用 Builder 的 `with_telemetry_sink(LoggingTelemetrySink(), ownership="owned")` 接入标准库日志；记录在 logger `pi_agent_py.telemetry` 的 `record.pi_agent` 字段里。应用配置日志级别和 handler；慢 handler 通过 `QueueHandler` 投递，保持观察调用快速返回。

安装 `pip install ".[otel]"` 后，从 `pi_agent_py.otel` 导入 `OtelTelemetrySink`，传入应用创建的 tracer 和 meter。实际导出建议用 SDK 的批处理器，应用负责 flush/shutdown。run/turn 从运行标识建立父子关系；模型请求下的 provider attempt 使用 envelope 的父 span ID。run settled 时清理尚未配对的 span；settled 后执行的插件通知是独立操作。指标使用少量固定维度；tool/provider 值只有出现在传入的 allowlist 才保留，其余归为 `other`。离线测试使用内存导出器和给定时间戳验证父子关系、耗时与强制结束。

## 从请求用量到账本

模型端口沿用 `TokenUsage`。每个逻辑模型请求发出最后一份报告，避免累计报告被加两次。请求失败或取消前已经收到的用量仍计入账本；没有报告时不猜测 token。成功 run 的 `AgentResult.usage` 保持原语义；`run_completed.payload["usage"]` 是该 run 的累计 token。`session.usage` 是本 Runtime 内跨 run 的 Session 汇总，控制器驱逐不会清除；`runtime.usage_ledger.for_run(session_id, run_id)` 可读取活动或近期 run。账本仅记录产出 `model_usage` 的请求；独立的压缩策略、未报告用量的请求和重启前的历史不在此范围内。

价格表按模型注入，单位为 USD/百万 token。Builder 从模型配置取模型名，手工注入 Provider 时可传 `with_provider(provider, model="...")`。缓存输入从普通输入中扣除后按缓存价计算；未配置缓存价时沿用输入价。缺价格的报告会增加 `unpriced_requests`，因此 `estimated_cost_usd` 可能只覆盖部分用量。完成事件以十进制字符串输出估算，适合 JSON；账本中用 `Decimal`。估算不作为账单依据。

## 练习

把示例的 `session.submit(..., max_buffer=1)` 延迟到结果结束后再消费，比较句柄的丢弃数和 Runtime 通用订阅收到的事件。再删除价格表中的模型条目，确认 token 总数不变而成本不可用。对应上游设计与本项目省略的跨进程重放、实时定价和 exporter 管理见[上游映射](../docs/upstream-mapping.md)。
