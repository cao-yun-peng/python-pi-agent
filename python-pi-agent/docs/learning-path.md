# Learning Path

每一阶段都必须形成可运行的纵向切片，而不是先造一批空接口。

1. **消息、流和最小循环**：理解一次 Agent run 如何开始、提交和 settled。
2. **真实 Provider 适配**：DeepSeek SSE、认证、错误映射、usage、thinking replay 和生命周期。
3. **工具调用**：schema、调度、tool result、失败和权限。
4. **会话与事件日志**：追加式存储、回放、恢复和分叉。
5. **上下文策略**：资源加载、token budget、裁剪与压缩。
6. **Provider 工程化**：重试、退避、限流、多供应商和模型能力。
7. **插件与 Hook**：观察、拦截、生命周期和释放。
8. **安全执行**：审批策略、工作区边界与沙箱。
9. **可观测性与评测**：trace、成本、延迟、回归数据集和评分器。
10. **产品适配与多 Agent**：CLI、Web/SSE、垂直 profile、委派和预算。

## 当前进度

- 阶段 1、2、3 已完成：最小循环、DeepSeek Provider 和顺序工具调用已经形成可运行纵向切片。
- 阶段 4 已完成内存与 JSONL `SessionStore`、乐观并发、跨 run/进程恢复、逐消息提交、event/run/turn/provenance、schema v4、durable event log、replay、带来源链接的持久化 fork 和 replayable compaction event；并完成 abort、steer、follow-up、settled/idle 控制状态机。durable 控制队列、多进程并发和数据库事务留给后续存储 Provider。
- 阶段 5 已完成 `ContextPlan`、异步 `ContextStrategy`、可替换 token 估算、完整 turn 分组、预算投影、手动 durable compaction，以及 run 前和工具续轮前的 token threshold 自动触发。
- 阶段 7 已完成第一条插件纵向切片：显式 factory/API、Tool 与 NextTurnHook 注册、事务化发布、priority 顺序、逐 Hook 超时、失败关闭、幂等注销和逆序 cleanup。观察订阅、session-aware durable hook、资源发现、runtime lease 与热重载仍未开始。
- 阶段 8 已完成第一条安全执行纵向切片：ToolExecutionContext 贯穿审批、Sandbox 与工具，默认 effect gate 拒绝 read/write/external，支持工具 allowlist、参数快照隔离、超时、取消和可观察拒绝。真正的子进程/容器隔离、工作区文件策略和身份化授权仍未开始。
- 阶段 9 已完成第一条安全观察纵向切片：Agent/Session/Plugin 统一隔离 EventSink 异常，日志事件可在入队前脱敏，慢 I/O 使用有界顺序 worker 并显式关闭。结构化 telemetry schema、指标聚合和 durable 审计 sink 仍未开始。

## 已完成学习节点：工具续轮前的 `NextTurnHook`

这个 Hook 服务于阶段 5 的自动压缩，但同时是进入阶段 7 前的第一份拦截型扩展样本。它刻意保持很小：Agent Loop 只提供安全时机，`AgentSession` 负责注入具体 compaction 行为。

```text
assistant tool call 已提交
  -> 所有 tool result 已提交
  -> NextTurnHook.prepare()
  -> CompactionPolicy 重新评估
  -> 可选的 durable compaction + replay
  -> ContextStrategy 生成请求投影
  -> 下一次模型请求
```

学习时按以下调用链阅读：

1. `ports.py::NextTurnHook`：先看输入、返回值和 awaited 拦截语义。
2. `runtime.py::Agent.run`：确认 Hook 只在工具结果全部提交后、且确实还有下一次请求额度时调用。
3. `runtime.py::Agent._prepare_next_turn`：观察取消传播、错误分类、返回值校验和内存历史替换。
4. `runtime.py::_SessionNextTurnHook`：理解通用 Hook 如何适配 session-specific compaction。
5. `runtime.py::AgentSession._maybe_auto_compact`：确认 run 前和工具续轮前复用同一 policy、strategy、store 与 replay 路径。
6. `tests/test_tool_runtime.py` 和 `tests/test_session_runtime.py`：用确定性证据核对正常、失败和取消路径。

这一节点的掌握标准：

- 能解释为什么 `EventSink` 是观察者，而 `NextTurnHook` 是可改变控制流的拦截器。
- 能说明 Hook 为什么必须位于完整 tool call/result 之后，而不能插在两者中间。
- 能画出 `Agent -> NextTurnHook -> AgentSession -> SessionStore -> replay -> Agent` 的状态回流。
- 能证明摘要失败或取消时，已提交工具结果仍然存在、没有 `context_compacted`，也不会发起第二次模型请求。
- 能解释为什么 Agent Loop 依赖 `NextTurnHook` 抽象，而不直接依赖 `CompactionPolicy` 或 `SessionStore`。

建议动手练习：

1. 给 `RecordingNextTurnHook` 记录 `next_request_number` 和收到的 message roles，手工预测测试结果后再运行。
2. 修改示例的消息计数阈值，让第一次评估为 `False`、工具结果后的评估为 `True`，观察 trace 顺序。
3. 让自定义 Hook 返回一个非 `Message` 值，确认 runtime 抛出稳定的 `ContextPreparationError`。
4. 在 compaction strategy 阻塞时取消 run，检查 durable log 中保留 user、assistant tool call 和 tool result，但没有 compaction event。

## 当前学习节点：从单 Hook 到插件能力容器

第 12 章把此前的单个 `NextTurnHook` 提升为可组合插件，但 Agent Loop 仍只看见原来的 `Tool` 和 `NextTurnHook` 端口：

```text
PluginFactory -> PluginAPI staging -> validate -> commit
                                      |
                                      v
                              PluginCapabilities
                               /              \
                         tools                ordered hook chain
                           |                         |
                           +--------> Agent <--------+
```

建议按以下顺序阅读：

1. `plugins.py::PluginAPI`：插件被允许做什么。
2. `_StagedPluginAPI` 与 `PluginHost.load()`：为什么失败不会产生半注册状态。
3. `PluginHost.snapshot()`：插件能力如何回到既有端口，而不是让 runtime 依赖插件容器。
4. `_PluginNextTurnHook.prepare()`：priority、链式输入、超时、错误和取消语义。
5. `PluginHost.unload()` / `close()`：能力撤销与资源释放为何是两个步骤。
6. `tests/test_plugins.py`：用端到端、回滚、超时和取消证据验证契约。

这一节点的掌握标准：

- 能从零写一个 factory，注册工具、Hook 和资源 cleanup。
- 能解释 staging/commit 与直接修改全局 registry 的差异。
- 能预测多个 Hook 的执行顺序，并说明前一 Hook 的结果如何流入后一 Hook。
- 能解释为什么拦截 Hook 要 fail-closed，而观察事件可以采用隔离错误策略。
- 能指出当前 snapshot 生命周期限制，以及为什么任意 Hook 不能直接改写 durable session history。

## 当前学习节点：隔离观察者与安全日志

第 13 章把 `EventSink` 的“不得改变执行结果”从注释变成运行时契约：

```text
runtime event
  -> automatic IsolatedEventSink
  -> explicit RedactingEventSink
  -> optional BufferedEventSink
  -> logger / telemetry exporter
```

建议按以下顺序阅读：

1. `ports.py::EventSink`：确认它是 observer，而不是 interceptor。
2. `events.py::IsolatedEventSink`：观察故障如何变成无敏感文本的 bounded diagnostics。
3. `events.py::redact_event`：确认复制、事件字段策略与嵌套 secret key 规则。
4. `events.py::BufferedEventSink`：理解有界队列、顺序 worker、drop 和 close 语义。
5. `runtime.py` / `plugins.py` 的 constructor：确认三个消费方建立同一隔离边界。
6. `tests/test_events.py`：用成功、原始失败、脱敏、下游错误和关闭路径核对契约。

这一节点的掌握标准：

- 能解释为什么 sink 的 `run_settled` 异常不能覆盖 Provider 错误。
- 能解释为什么日志要先脱敏再入队，以及原始 UI 事件为何不应被全局裁剪。
- 能区分 `event_sink_failures` 与 `buffered.failures` 的失败阶段。
- 能说明 queue full 时允许 drop 的前提：恢复事实已经进入 `SessionStore`。
- 能证明 `NextTurnHook` 仍然 fail-closed，不受观察者隔离策略影响。

## 下一步

会话方向的下一步不应直接扩展多 Agent，而应先决定控制队列是否需要跨进程 durability；若需要，必须引入 queued/claimed/consumed/cancelled 事实、lease 和幂等恢复，不能把当前内存 deque 直接序列化。

插件方向的下一小步应设计 **session-aware context hook**：只变换单次 `ContextPlan`，不破坏 durable replay。之后再考虑 runtime lease 和安全的热卸载，不应直接做目录自动扫描。

可观测性方向下一步应先定义稳定 telemetry schema 和低基数指标，再决定是否需要强一致审计 sink。强一致审计不能伪装成 best-effort EventSink，而应成为独立 durable capability。

安全执行方向下一步应实现一个真正隔离的 workspace Sandbox provider：明确进程边界、路径规范化、环境变量 allowlist、网络策略、资源上限和终止协议。不能仅凭 `side_effects` 声明信任未知插件代码。

上下文方向仍应实现 compaction eval 的最小离线切片，用版本化 case 和 deterministic grader 验证摘要保留目标、约束、工具发现和下一步；随后再处理精确 tokenizer 与资源加载。
