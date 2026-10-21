# 第十三章：隔离 EventSink 与安全日志

## 1. 问题不只是“日志写失败”

`EventSink` 会收到完整的运行轨迹，其中 `message_committed` 包含消息正文、thinking、工具参数和结构化工具结果。旧实现直接同步调用外部 sink：

```python
self._event_sink.emit(
    AgentEvent(kind=kind, channel=channel, payload=payload)
)
```

这同时产生两个风险：

1. sink 抛出的异常会进入 Agent 的控制流。`run_started` 失败会阻止 run，`run_settled` 失败还可能覆盖原始 Provider 异常。
2. 应用若把事件原样写入日志，会把 prompt、模型 thinking、工具凭证和工具结果一起落盘。

观察者应当报告运行，而不是决定运行是否成功。日志也不应默认获得模型可见的完整数据。

## 2. 四层职责

安全日志链路由四个小对象组合，不把日志框架塞进 Agent Loop：

```text
Agent / AgentSession / PluginHost
  -> IsolatedEventSink（runtime 自动添加，吞掉观察者异常并记录安全诊断）
  -> RedactingEventSink（复制事件并脱敏）
  -> BufferedEventSink（有界队列，后台顺序投递）
  -> CallbackEventSink（真正的 JSON/logger/telemetry 写入）
```

`IsolatedEventSink` 保留同步的正常投递顺序，但捕获普通 `Exception`。它只记录 event kind、失败阶段、异常类型和是否丢弃，不复制异常文本，因为异常文本本身也可能包含 token 或用户数据。

`Agent`、`AgentSession` 和 `PluginHost` 都通过 `ensure_isolated_event_sink()` 建立同一个边界；已经隔离的 sink 不会被重复包装。失败可以从 `event_sink_failures` 查询，但不会改变模型、工具、持久化或插件结果。

## 3. 脱敏是日志适配策略，不是修改领域事件

默认策略处理以下模型可见字段：

- `text_delta.text` 与 `message_committed.content`
- `thinking_delta.text` 与 `message_committed.thinking`
- `tool_call_received.arguments` 与 committed tool calls 的 arguments
- `message_committed.structured_content`
- 任意嵌套 mapping 中常见的 secret/password/authorization/cookie/token/api_key 键

token 用量字段（如 `input_tokens`、`total_tokens`、`token_budget`）在允许列表中，不会因名字含有 token 被误删。

`redact_event()` 创建新的 `AgentEvent` 和 payload 容器，不修改 runtime 原事件。因此 UI 或测试仍可显式消费原始事件；只有日志链路采用脱敏视图。若业务确认某类正文可记录，可以传自定义策略：

```python
policy = EventRedactionPolicy(redact_message_content=False)
sink = RedactingEventSink(buffered, policy=policy)
```

即使关闭整段正文脱敏，嵌套的 `access_token`、`password` 等键仍会被递归替换，除非显式关闭 `redact_sensitive_keys`。

## 4. 慢 I/O 不占用 Agent Loop

`BufferedEventSink.emit()` 只做 `put_nowait()`，后台 worker 按顺序调用下游 sink。同步 logger 通过 `asyncio.to_thread()` 执行，不阻塞 Agent 所在事件循环。

队列是有界的。满队列不会反压或终止 Agent，而是记录一条 `enqueue/QueueFull/dropped=True` 诊断。这意味着该通道是 **best-effort observation**，不是 durable log；模型可见事实仍必须写入 `SessionStore`。

后台 worker 是显式资源，应用必须释放：

```python
buffered = BufferedEventSink(CallbackEventSink(write_json_log))
logging_sink = RedactingEventSink(buffered)
agent = Agent(provider, event_sink=logging_sink)

try:
    result = await agent.run("question")
    await buffered.flush()
finally:
    await buffered.aclose()
```

推荐把 `RedactingEventSink` 放在 `BufferedEventSink` 外面，使敏感原文在入队前就被替换。`aclose()` 会先排空队列再停止 worker，可重复调用；关闭后的事件会被丢弃并留下诊断。

## 5. 与 Pi 的对应和主动差异

对照 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`：

- `packages/agent/src/agent.ts::subscribe()` 的 listener promise 会按订阅顺序 await，并计入 run settlement；这是可等待 listener 契约。
- `packages/coding-agent/src/core/agent-session.ts::_emit()` 同步遍历公开 session listener。
- Pi 的 extension runner 另有 awaited handler，部分 handler 会影响行为。

本项目没有把这些语义全部合并到 `EventSink`。`EventSink` 专门代表不可改变执行结果的观察者，所以 runtime 自动隔离失败；能够修改或拒绝行为的 `NextTurnHook` 仍然 awaited、带超时并 fail-closed。这个差异让教学代码能明确回答“这是 trace，还是控制点”。

## 6. 验证与练习

运行：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\13_safe_event_logging.py
.\.venv\Scripts\python.exe -m unittest python-pi-agent/tests/test_events.py -v
```

应观察到日志中的正文被替换为 `<redacted>`，而最终 `AgentResult.message.content` 仍是原始回答。

练习：

1. 让 callback 抛出 `OSError`，确认 Agent 成功且 `event_sink_failures` 或 `buffered.failures` 有诊断。
2. 把 `max_queue_size` 改为 1 并制造慢 logger，观察丢弃语义。
3. 调整 `EventRedactionPolicy`，证明原始 `AgentEvent` 没有被修改。
4. 去掉 `aclose()`，解释为什么显式生命周期是插件和后台任务的必要契约。

掌握标准：能区分 durable state、best-effort observer 和 awaited interceptor；能解释为何先脱敏再入队，以及为何失败诊断不能保存原始异常消息。
