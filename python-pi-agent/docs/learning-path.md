# Learning Path

每一阶段都必须形成可运行的纵向切片，而不是先造一批空接口。

1. **消息、流和最小循环**：理解一次 Agent run 如何开始、提交和 settled。
2. **真实 Provider 适配与组装**：DeepSeek/Kimi/GLM Chat Completions 与 TiMi Responses 的认证、错误映射、模型能力、续接状态和生命周期，以及显式 YAML/dotenv 配置边界。
3. **工具调用**：schema、调度、tool result、失败和权限。
4. **会话与事件日志**：追加式存储、回放、恢复和分叉。
5. **上下文策略**：资源加载、token budget、裁剪与压缩。
6. **Provider 工程化**：重试、退避、限流、多供应商和模型能力。
7. **插件与 Hook**：观察、拦截、生命周期和释放。
8. **安全执行**：审批策略、工作区边界与沙箱。
9. **可观测性与评测**：trace、成本、延迟、回归数据集和评分器。
10. **产品适配与多 Agent**：CLI、Web/SSE、垂直 profile、委派和预算。

## 当前进度

- 阶段 1、2、3 已完成：最小循环、DeepSeek/Kimi/GLM Chat Completions、TiMi Codex/GPT/Grok Responses、共享 SSE framing、强类型 YAML/dotenv 组装和顺序工具调用已经形成可运行纵向切片。
- 阶段 4 已完成内存与 JSONL `SessionStore`、乐观并发、跨 run/进程恢复、逐消息提交、event/run/turn/provenance、schema v6、durable event log、replay、持久化 fork、compaction event、provider-owned state，以及 steer/follow-up 的 enqueue/claim/deliver/cancel 恢复语义。多进程文件锁和数据库事务留给后续存储 Provider。
- 阶段 5 已完成 `ContextPlan`、异步 `ContextStrategy`、可替换 token 估算、完整 turn 分组、预算投影、手动 durable compaction，以及 run 前和工具续轮前的 token threshold 自动触发。
- 阶段 6 已完成第一条 Provider resilience 纵向切片：显式候选、能力与 provider-state 路由、pre-output retry、full-jitter/Retry-After backoff、兼容 fallback、总 attempt/elapsed budget、取消传播和 attempt trace。Circuit breaker、健康/成本/延迟加权与跨模型 state 转换留给有运行数据后的迭代。
- 阶段 7 已完成显式 factory/API、Tool 与 NextTurnHook 注册、事务化发布、priority 顺序、逐 Hook 超时、失败关闭、幂等注销、SessionPluginFactory/Lease、请求投影 Hook、后台 settled observer 和逆序 cleanup。资源发现、durable control port 与安全热重载仍未开始。
- 阶段 8 已完成两条安全执行纵向切片：第一条由 ToolExecutionContext、ApprovalPolicy 与 ToolExecutionGate 提供声明式默认拒绝；第二条已经演进为 `BashTool -> SandboxedBashExecutor -> DockerProcessSandbox`，包含 workspace 相对工作目录、超时封顶、固定 Bash argv、每流有界输出尾部、fail-closed 和清理证据。兼容期 `SandboxedShellTool` 仍可用；文件 read/search/patch 的统一 workspace port、完整输出 spill、单次权限升级和身份化授权仍未开始。
- 阶段 9 已完成两条观察纵向切片：第一条统一隔离 EventSink 异常、入队前脱敏和有界 worker 生命周期；第二条增加 versioned AgentEventEnvelope、固定 JSON record、模型/工具/压缩/Hook span duration，以及 observer message 到 durable event 的 identity join。低基数指标聚合、采样/exporter 和 durable 审计 sink 仍未开始。
- 阶段 10 已完成第一条应用组装纵向切片：`AgentProfile`、显式 `ProviderCatalog`、事务化 `AgentRuntimeBuilder`、owned/borrowed 生命周期、`AgentRuntime` facade、可复用 `RuntimeSession` lease，以及惰性 TTL/LRU controller 驱逐。正式 CLI、EventHub/RunHandle、多租户 SessionRef 与 Web/SSE 尚未开始。
- Provider 调用已增加 request/attempt/purpose/session/run/turn 上下文；Chat Completions 三适配器复用内部 wire codec，Responses 仅复用 SSE framing，并通过 provider-owned state 完成无服务端存储的续轮。`ResilientModelRuntime` 已在 wire adapter 之上复用这些边界，不合并两套协议。

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

应用层下一步先做正式 CLI，验证配置覆盖、Profile 选择、信号取消、退出码和 Runtime 关闭契约；之后再抽取 EventHub、RunHandle 与多租户 `SessionRef`，最后让 Web/SSE 只依赖这些稳定接口。HTTP handler 不得直接持有 `AgentSession`，断线也不能隐式删除 durable session。

Provider resilience 的下一步不是继续堆策略，而是先用现有 attempt trace 建立低基数成功率、错误率和延迟证据，再决定是否引入 circuit breaker、健康/成本加权及半开探测。Partial stream 仍然 fail closed；在定义客户端 reset 协议前不自动续跑。

插件方向下一步若需要主动控制能力，应给 SessionPluginContext 注入受限 control port，并复用 durable enqueue/claim/deliver/cancel；不得暴露 AgentSession、Store 或内存 deque。之后再考虑资源发现与安全热重载，不应直接做目录自动扫描。

可观测性方向下一步应在现有稳定 envelope 上实现最小低基数聚合：run 成功率、模型/工具/压缩耗时和 observer drop 计数；随后再决定 exporter 与采样策略。强一致审计不能伪装成 best-effort EventSink，而应成为独立 durable capability。

安全执行方向下一步先把 `SandboxedBashExecutor` 放入显式应用配置与 Profile 组装，并为截断输出设计 workspace 内可恢复的 spill 生命周期；随后让 read/search/patch 共用规范化 workspace port，再设计 `read-only -> workspace-write` 的单次升级审批。不要为追求形式完整同时实现多套 OS-native runner，也不要把未知 Python 插件误认为已被 Shell sandbox 覆盖。

上下文方向仍应实现 compaction eval 的最小离线切片，用版本化 case 和 deterministic grader 验证摘要保留目标、约束、工具发现和下一步；随后再处理精确 tokenizer 与资源加载。
