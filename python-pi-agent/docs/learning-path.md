# Learning Path

租约与心跳练习：阅读[第 51 章](../tutorials/51-redis-leases-heartbeat.md)，先用手动时钟验证互斥、续期超时、失租取消与零剩余任务，再运行示例 54 的成功/失租模式。解释 Redis key 丢失为何不能越过 PG 当前 grant，比较终态 finalize 与续期的顺序，说明客户端和作用域各自的关闭责任。Runtime 自动接线与恢复扫描留到后续阶段。

执行存储练习：阅读[第 50 章](../tutorials/50-postgres-execution-storage.md)并运行 FakeProvider 示例 53，比较当前历史与原操作回执。解释为什么完整请求摘要要包含实际消息、为什么 COMMIT 回复丢失不能重跑工具，再验证旧 epoch 和事务取消回滚。合成协调租约只作本章边界替身，真实 Redis/心跳见第 51 章，Runtime 接线仍按后续步骤完成。

数据库存储练习：完成共享 Codec 后阅读[第 49 章](../tutorials/49-postgres-session-store.md)，使用自己准备的 PG 数据库显式迁移，运行 FakeProvider 示例 52，再用独立连接池验证冲突、回滚、重开与 fork。理解基础 SessionStore 与独立执行租约控制面的区别。

会话格式练习：阅读第 48 章并运行示例 51，观察完整事件 round-trip 和旧 schema 拒绝；再运行 JSONL 与模型请求快照测试，解释格式复用为何没有赋予文件多进程写入能力。

每一阶段都必须形成可运行的纵向切片，而不是先造一批空接口。

分布式执行的首个机制切片见[第 47 章](../tutorials/47-execution-journal-contracts.md)和示例 50：先复习第 17 章 durable controls，再运行纯执行日志轨迹，练习 delivered 与 completed、checkpoint、预算不重置和旧许可拒绝。此时不需要 PG/Redis；真实存储与 Runtime 接线按[开发规格](design/phase-1-distributed-session-storage.md)后续步骤完成，不能把纯 replay 当成事务已经落盘。

1. **消息、流和最小循环**：理解一次 Agent run 如何开始、提交和 settled。
2. **真实 Provider 适配与组装**：DeepSeek/Kimi/GLM Chat Completions 与 TiMi Responses 的认证、错误映射、模型能力、续接状态和生命周期，以及显式 YAML/dotenv 配置边界。
3. **工具调用**：schema、调度、tool result、失败和权限。
4. **会话与事件日志**：追加式存储、回放、恢复和分叉；JSONL 仅接受当前 schema，练习验证旧版拒绝与字段完整性，不维护开发期兼容分支。
5. **上下文策略**：资源加载、token budget、裁剪与压缩。
6. **Provider 工程化**：重试、退避、限流、多供应商和模型能力。
7. **插件与 Hook**：观察、拦截、生命周期和释放。
8. **安全执行**：审批策略、工作区边界与沙箱。
9. **可观测性与评测**：trace、成本、延迟、回归数据集和评分器。
10. **产品适配与多 Agent**：CLI、Web/SSE、垂直 profile、委派和预算。

## 当前进度

- 应用层归组后，按 `application/configuration.py` → 两个 Catalog → `application/builder.py` → `application/runtime.py` 阅读加载、初始化与 Session 池，再通过 `application/lifecycle.py` 理解所有权和逆序关闭。共同消费的规格位于 `runtime/components.py` 和 `runtime/session_spec.py`。对照配置、Catalog、Lifecycle、Runtime 和 SessionSpec 测试验证取消、回滚与关闭顺序。

- 第一批功能归组后，从 `storage/memory.py` 和 `storage/jsonl.py` 阅读存储，从 `context/strategies.py` 和 `context/compaction.py` 阅读上下文，从 `observability/` 阅读事件、遥测与用量。现有测试覆盖新实现，原具体模块路径已移除。

- 工具能力从 `tools/function.py` 阅读函数适配，经 `tools/validation.py`、`tools/approval.py` 和 `tools/gates.py` 理解参数校验、审批与执行策略，再沿 `tools/shell.py` → `tools/sandbox.py` 跟踪 Bash 请求与 Docker 隔离。对照 `test_tool_runtime.py`、`test_shell.py` 和 `test_sandbox.py` 验证拒绝、超时与取消；配置注册仍从 `application/capabilities/bash.py` 进入。

- 第三批归组后，从 `providers/runtime.py` 跟踪逻辑请求、路由、重试与物理 attempt，再从 `runtime/run_handle.py` 区分 run 取消、等待者取消和事件流关闭。对照 `test_model_runtime.py` 与 `test_run_observation.py` 验证流开始后的重试限制和定向取消。

- 阶段 1、2、3 已完成：最小循环、DeepSeek/Kimi/GLM Chat Completions、TiMi Codex/GPT/Grok Responses、共享 SSE framing、强类型 YAML/dotenv 组装和顺序工具调用已经形成可运行纵向切片。
- 阶段 4 已完成内存与 JSONL `SessionStore`、乐观并发、跨 run/进程恢复、逐消息提交、event/run/turn/provenance、schema v6、durable event log、replay、持久化 fork、compaction event、provider-owned state，以及 steer/follow-up 的 enqueue/claim/deliver/cancel 恢复语义。多进程文件锁和数据库事务留给后续存储 Provider。
- 阶段 5 已完成 `ContextPlan`、异步 `ContextStrategy`、可替换 token 估算、完整 turn 分组、预算投影、手动 durable compaction，以及 run 前和工具续轮前的 token threshold 自动触发。
- 阶段 6 已完成第一条 Provider resilience 纵向切片：显式候选、能力与 provider-state 路由、pre-output retry、full-jitter/Retry-After backoff、兼容 fallback、总 attempt/elapsed budget、取消传播和 attempt trace。Circuit breaker、健康/成本/延迟加权与跨模型 state 转换留给有运行数据后的迭代。
- 阶段 7 已完成显式 factory/API、Tool 与 NextTurnHook 注册、事务化发布、priority 顺序、逐 Hook 超时、失败关闭、幂等注销、SessionPluginFactory/Lease、请求投影 Hook、后台 settled observer 和逆序 cleanup。资源发现、durable control port 与安全热重载仍未开始。
- 阶段 8 已完成两条安全执行纵向切片：第一条由 ToolExecutionContext、ApprovalPolicy 与 ToolExecutionGate 提供声明式默认拒绝；第二条已经演进为 `agent.yaml -> coding_profile -> BashTool -> SandboxedBashExecutor -> DockerProcessSandbox`，包含 workspace 相对工作目录、超时封顶、固定 Bash argv、每流有界输出尾部、网络副作用声明、fail-closed 和清理证据。兼容期 `SandboxedShellTool` 仍可用；文件 read/search/patch 的统一 workspace port、完整输出 spill、单次权限升级和身份化授权仍未开始。
- 阶段 9 已完成 EventSink 隔离、脱敏和背压，统一了 envelope 和 span 耗时，并增加 Runtime 的订阅、低基数指标和用量账本。durable 审计仍是独立任务。
- 阶段 10 已完成 Profile、显式 Catalog、事务化 Builder、owned/borrowed 生命周期、Runtime facade、Session lease 与惰性 TTL/LRU。正式 CLI、多租户 SessionRef 与 Web/SSE 尚未开始。
- Provider 调用已增加 request/attempt/purpose/session/run/turn 上下文；Chat Completions 三适配器复用内部 wire codec，Responses 仅复用 SSE framing，并通过 provider-owned state 完成无服务端存储的续轮。`ResilientModelRuntime` 已在 wire adapter 之上复用这些边界，不合并两套协议。

- 基座路线图 A1/A2 已落地。用 [run 失败分类练习](../tutorials/13-safe-event-observation.md#8-run-失败分类) 跟踪 Store 失败从原生异常到调用方错误和 live 事件的过程，再观察 Session 如何继续处理后续 run。
- 基座阶段 1 的组装链路已接入 `AgentComponents`。用 [Profile 与统一生命周期](../tutorials/25-application-runtime-assembly.md#3-builder-与-profile-的区别) 跟踪组件如何从 factory 解析到 Agent 实际消费，并比较常驻 Runtime 和独立 Agent 的入口。
- 下游应用从 `pi_agent_py.api` 导入组装与扩展契约。阅读 [稳定入口回归](../tests/test_api.py)，练习只通过公开导入接入自定义 Provider 和工具插件。
- Runtime 拆分后，从 `runtime/control.py` 追踪一个排队 run，进入 `runtime/session.py` 恢复历史和装配，再到 `runtime/loop.py` 运行模型和工具。对照 [Session 控制测试](../tests/test_session_control.py) 观察取消和队列恢复。
- 基座阶段 3 B2 已完成。阅读[第 30 章](../tutorials/30-session-spec.md)，从 SessionOpenRequest 追踪 resolver、池内规格比较和 metadata envelope，再运行双角色示例验证工具选择、冲突拒绝和观察范围。
- 基座阶段 3 B3 已完成。阅读[第 31 章](../tutorials/31-registered-provider-settings.md)，沿 YAML → parser → Settings → 凭据 → Config → factory 跟踪外部 Provider，并通过 src 外的测试适配器验证取消和回滚。
- 基座阶段 3 B4 已完成。阅读[第 32 章](../tutorials/32-configured-capabilities.md)，沿 capabilities → Settings → PluginAPI → PluginHost snapshot 跟踪工具和 Hook，再验证初始化取消与逆序回滚。比较 Bash 两种配置形式得到的统一序列。

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
2. `runtime/loop.py::Agent.run`：确认 Hook 只在工具结果全部提交后、且确实还有下一次请求额度时调用。
3. `runtime/loop.py::Agent._prepare_next_turn`：观察取消传播、错误分类、返回值校验和内存历史替换。
4. `runtime/compaction_orchestration.py::_SessionNextTurnHook`：理解通用 Hook 如何适配 session-specific compaction。
5. `runtime/compaction_orchestration.py::_SessionCompactor.maybe_auto_compact`：确认 run 前和工具续轮前复用同一 policy、strategy、store 与 replay 路径。
6. `tests/test_tool_runtime.py` 和 `tests/test_session_runtime.py`：用确定性证据核对正常、失败和取消路径。

这一节点的掌握标准：

- 能解释为什么 `EventSink` 是观察者，而 `NextTurnHook` 是可改变控制流的拦截器。
- 能说明 Hook 为什么必须位于完整 tool call/result 之后，而不能插在两者中间。
- 能画出 `Agent -> NextTurnHook -> _SessionCompactor -> SessionStore -> replay -> Agent` 的状态回流。
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
2. `observability/events.py::IsolatedEventSink`：观察故障如何变成无敏感文本的 bounded diagnostics。
3. `observability/events.py::redact_event`：确认复制、事件字段策略与嵌套 secret key 规则。
4. `observability/events.py::BufferedEventSink`：理解有界队列、顺序 worker、drop 和 close 语义。
5. `runtime/loop.py`、`runtime/session.py` 和 `plugins.py` 的构造入口：确认三个消费方建立同一隔离边界。
6. `tests/test_events.py`：用成功、原始失败、脱敏、下游错误和关闭路径核对契约。

这一节点的掌握标准：

- 能解释为什么 sink 的 `run_settled` 异常不能覆盖 Provider 错误。
- 能解释为什么日志要先脱敏再入队，以及原始 UI 事件为何不应被全局裁剪。
- 能区分 `event_sink_failures` 与 `buffered.failures` 的失败阶段。
- 能说明 queue full 时允许 drop 的前提：恢复事实已经进入 `SessionStore`。
- 能证明 `NextTurnHook` 仍然 fail-closed，不受观察者隔离策略影响。

## 下一步

应用层下一步用正式 CLI 验证配置覆盖、Profile 选择、信号取消、退出码和 Runtime 关闭契约，再让 Web/SSE 通过已有的 EventHub 与 RunHandle 消费事件。HTTP handler 不得直接持有 `AgentSession`，断线也不能隐式删除 durable session。

Provider resilience 的下一步不是继续堆策略，而是先用现有 attempt trace 建立低基数成功率、错误率和延迟证据，再决定是否引入 circuit breaker、健康/成本加权及半开探测。Partial stream 仍然 fail closed；在定义客户端 reset 协议前不自动续跑。

插件方向下一步若需要主动控制能力，应给 SessionPluginContext 注入受限 control port，并复用 durable enqueue/claim/deliver/cancel；不得暴露 AgentSession、Store 或内存 deque。之后再考虑资源发现与安全热重载，不应直接做目录自动扫描。

完成[第 29 章](../tutorials/29-run-observation.md)，验证两个并发 run 互不串流、慢订阅者不拖住 dispatcher，并用内存导出器重建父子 span。用 FakeProvider 分别观察正常、取消和缺价的用量汇总。强一致审计应作为独立 durable capability。

后续练习按[可观测层补充设计](design/observability-evolution.md)推进：先核对九阶段事件与错误分类投影，再验证观察者值隔离与投递健康，最后用假时钟验证性能指标、绑定外部父 trace。这些为待实现目标；能够指出 settled、完整事件流、完整用量与完整计费之间的区别，才算掌握现有观察边界。

安全执行方向下一步为截断输出设计 workspace 内可恢复的 spill 生命周期，明确路径、权限、清理和模型可见性；随后让 read/search/patch 共用规范化 workspace port，再设计 `read-only -> workspace-write` 与 network 的单次升级审批。不要为追求形式完整同时实现多套 OS-native runner，也不要把未知 Python 插件误认为已被 Shell sandbox 覆盖。

上下文方向仍应实现 compaction eval 的最小离线切片，用版本化 case 和 deterministic grader 验证摘要保留目标、约束、工具发现和下一步；随后再处理精确 tokenizer 与资源加载。

## B6：从函数签名到工具契约

阅读[第 33 章](../tutorials/33-tool-decorator.md)，运行示例 34，跟踪 schema 推导 → runtime 校验 → context 注入 → ToolOutput。用离线测试区分 ToolError、默认审批拒绝、Gate 拒绝、超时和取消。复杂类型推导仍留待后续；可插拔校验器见第 35 章。

## B5：组合请求与工具结果变换

阅读[第 34 章](../tutorials/34-middleware.md)，运行示例 35。先追踪请求投影，再追踪工具执行后的结果变换和 durable 提交；对照失败、取消和恢复。通过 tests/downstream_middleware 比较 Builder 与 Plugin 接入，并解释 SessionPlugin 和 NextTurnHook 为何保留独立语义。

## MCP 阶段 0：通用工具适配接缝

阅读[第 35 章](../tutorials/35-generic-tool-adapter-seams.md)，安装 [jsonschema] 后运行示例 36。先对比 supports 与 validate 的职责，再证明复杂 schema 在审批前生效、跨任务身份一致、插件借观察端口不拥有关闭权。最后用零超时与取消区分未知结果和未提交结果。MCP 的 Fake 发现进入下一章，真实传输仍待后续。

## MCP 阶段 1：Fake 端到端切片

阅读[第 36 章](../tutorials/36-mcp-fake-tools.md)，运行示例 37。沿目录发现 → allowlist/校验 → 审批/Gate → 一次原名调用 → 结果投影/B5 → durable 续轮追踪；分别移除三个授权条件，验证零调用。用错误输出、零期限和 Event 取消证明 unknown、不重发与恢复；解释为什么 Fake Lock 无法证明真实 SDK owner 的任务与子进程生命周期。参考 ADR 0036。

## MCP 阶段 2：SDK owner 与 stdio

阅读[第 37 章](../tutorials/37-mcp-sdk-transports.md)，运行示例 38，再显式开启本地集成。用不同任务执行 build/run/close，解释同任务 SDK enter/exit、有界排队与取消隔离；同时区分 Python Task 完成与实际父/子进程退出。

## MCP 阶段 3：HTTP 与接收预算

继续[第 37 章](../tutorials/37-mcp-sdk-transports.md)的 HTTP 小节，运行示例 39。对照解析前字节、单 SSE event 与解析后结构化预算；用 401、断流、重定向、超大 JSON/SSE 验证固定 unknown、只提交一次以及关闭正常。

## MCP 阶段 4：配置与教学交付

阅读[第 38 章](../tutorials/38-mcp-configured-capability.md)，运行示例 40。沿 Settings → 显式引用 → 目标策略 → 插件事务 → 普通 Tool → durable 结果 → 关闭追踪；证明缺 Catalog/引用/策略时无连接，并能够替换 Connector/SecretResolver 而不改主循环。


## 练习：默认值也是工具契约

运行示例 34，观察错误默认值在定义时被拒绝，以及两次 collect_tags 调用得到相同的独立结果。阅读教程 33 和 ADR 0034，解释显式 null 与省略的区别，以及为什么需要私有快照和每次深拷贝。运行 test_tool_decorator，确认双 Agent、失败及取消都不会污染下一次默认值；直接调用 .fn 仍遵循普通 Python 语义。


## 练习：工具定义快照

运行示例 41，修改原 schema 和工具 definition，比较旧 Agent 与新 Agent 的结果和既有 tool_validation_failed/tool_execution_* 事件。阅读第三章新增小节与 ADR 0040，解释为什么隔离嵌套值和固定运行时绑定缺一不可，以及为什么不能 deepcopy handler/连接。通过 test_tool_definition_snapshots 验证审批、Gate、超时和取消；区分 Agent 生命周期与 Session 每个 run 的组装边界。


## Workspace/read 纵向切片

阅读第 39 章，查看 tools/workspace/ 下的 read.py 与 sandbox_backend.py，运行示例 42：先以内存目录观察分页、版本、缺失文件和模型续轮，再用显式本地 Python Docker 镜像读取真实 workspace。解释路径规范化与 Linux no-follow 打开的职责、原始文件预算与输出页预算的区别，并通过 test_workspace_read 证明 Gate/审批零访问、取消和符号链接/FIFO 拒绝。read_only_profile 放行注册的只读文件工具；接着学习第 40 章的修改与查询契约。


## Workspace 修改/查询练习

运行示例 43，观察 ls/find/grep → 创建 → 精确编辑 → 旧版本拒绝 → 读取。解释 allow_write、审批与 Gate 的独立职责，证明结果未知不会重做写入；用 test_workspace_tools 和 test_sandbox 验证 stdin、版本、取消队列、临时清理及查询预算。下一阶段再考虑 YAML 文件能力配置、regex/.gitignore 与跨实例一致性。


## 契约归属与依赖检查

阅读[第 41 章](../tutorials/41-contract-ownership-and-dependencies.md)与 ADR 0043，先用 ports 的策略输入/输出解释接口所有权，再追踪 SessionSpecResolver 为何属于会话规格层。运行 test_architecture、test_port_contracts、test_model_runtime、test_session_spec 与 test_api；示例 23 和 31 分别观察重试/回退事件和会话能力选择。练习仅在测试字符串中加入 TYPE_CHECKING 反向引用，确认检查能失败；说明安全默认与压缩编排例外为何按消费者精确登记。

## 模型请求记录与进程退出后的还原

阅读[第 42 章](../tutorials/42-recorded-model-requests.md)，运行示例 44 的 record 阶段，删除 reference.txt，再在另一个进程运行 restore。比较 Session 历史与请求快照，确认临时检索只进入后者。用 test_model_request_recording 追踪提交成功、存储失败、重复身份、重试与取消，并解释为何快照存在不能推出远端已收到请求。随后可基于这些固定输入开展离线评测，不必重跑原插件。


## 静态系统提示词练习

阅读[第 43 章](../tutorials/43-static-system-prompts.md)，运行示例 45。添加一个显式工具规则，对比 minimal/coding、完整字符串覆盖和 None 禁用。解释为什么渲染必须晚于插件合并与 SessionSpec，以及修改默认值为什么不会更新旧历史。通过 test_system_prompts、test_session_spec 和请求快照兼容测试验证。


## 结构化摘要练习

阅读[第 44 章](../tutorials/44-structured-compaction-prompts.md)，运行示例 46，追踪 system 摘要规则、user 历史数据、空工具列表、durable checkpoint 与压缩事件。解释计划与已完成的区别、未知副作用为什么不能安全重试。运行 test_compaction、test_system_prompts 和 test_session_runtime；区分请求契约验证与真实模型摘要质量评测。


## SWE-bench 评测切片

阅读第 45 章，先运行示例 47 demo，查看两次 requests、tool result、patch 和 resolved=null。再固定 HF revision 选择开发题，验证 gold baseline 后接入模型。失败时区分环境、Runtime、补丁应用和测试判分；取消必须关闭任务容器。默认测试 test_swebench_evaluation 不下载数据或访问模型；真实评测须安装独立工具并显式准备镜像。


Windows 练习：比较 CRLF 失败日志与 LF 修复后的 gold baseline，解释为什么评分进程退出 0 不等于任务 resolved，以及为什么必须先验证参考补丁。第 45 章记录固定数据版本、镜像 digest 和环境验证结果。


## 串行批次与预算恢复

第 46 章和示例 48 将单题接缝组合为批次。先用 FakeProvider/fake clock 验证共享 limiter、pre-output retry、取消后的 checkpoint、预算不重置和固定分母；再用真实 Docker 验证 base-only 历史及补丁恢复。正式 batch 先逐题 gold control，再运行模型，最后独立评分。练习从 runtime_error 与 resolved=true 的并存解释执行和任务正确性，并验证 --resume 不重跑已评分失败题。

环境练习：构建 evaluation/images/pydicom-1139-pytest7，比较同一个旧 hooks 探针在 pytest 8 与 7 的表现；用 prepare.py 生成解题/判分共用的镜像输入，再比较参考补丁全过与未修复对照仅新增功能测试失败。解释为什么不能删掉失败测试、改写原批次或只给 Agent 容器降级依赖。实测证据见第 46 章环境修复补充。

失败轨迹练习：运行示例 49，从 requests 中定位剩余预算，确认 session 中没有临时预算消息；比较编辑返回 0 与源码语法正确的区别。用 print-only、assert True、原始失败/候选通过和验证后改变补丁四种情况理解 verify。test_evaluation_workflow 与真实 Docker 检查覆盖这些接缝；详细边界见第 46 章与 ADR 0048 补充。
