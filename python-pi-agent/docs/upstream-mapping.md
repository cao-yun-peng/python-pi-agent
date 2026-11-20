# Upstream Mapping

本项目是独立 Python 实现。映射用于学习设计，不承诺 API 兼容。

当前 Pi 参考固定在 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`。
DeepSeek Harness Shell/沙箱概念对照 commit `f3803c96974ad4a94195fb97e5c637e9c2a3afe3` 的 `packages/shell`、`packages/subprocess` 与 `packages/sandbox`；只借鉴 request/spec、能力接缝、per-call policy、fail-closed 与执行证据，不复制其多包结构。

| 本项目 | Pi 概念 | DeepSeek Harness 思想 | 当前差异 |
|---|---|---|---|
| MCP YAML capability | 固定 Pi McpServerRegistry 的配置/目录组装职责 | 显式 Definition/Provider/Consumer/Lifecycle/Evidence | Python 复用 CapabilityCatalog 与不可变 Settings，秘密引用由应用解析，默认不注册/授权；不复制多层产品配置、OAuth 或热重载 |
| MCP Streamable HTTP | 固定 Pi McpServerRegistry 的 HTTP 工具接入职责 | 安全接缝、Provider 所有权与证据 | Python 使用独立 SDK httpx2 client、预解析响应预算和显式凭据；不复制 OAuth、自动恢复或动态 exposure |
| MCP SDK owner 与 stdio | 同一固定 Pi commit 的 McpServerRegistry 生命周期职责 | 可替换 Provider、受控并发与释放证据 | Python 固定官方 SDK 2.3.0；owner 独立于调用任务，以 Session.send_request 避免高层重发。验证实际 Windows 进程树；不提供主机沙箱或自动重连 |
| `tools/mcp/`、FakeMcpConnector | Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 `packages/coding-agent/src/core/mcp-servers.ts::McpServerRegistry` 与 `packages/coding-agent/docs/mcp.md` | Definition/Provider/Consumer/Lifecycle/Evidence 能力接缝 | Python 阶段 1 用 SDK 无关 DTO、显式插件事务与普通 Tool 验证发现到 durable 续轮；默认拒绝连接、审批/Gate 独立、不自动重发。未实现 SDK owner、真实传输、分页、队列、OAuth 或动态 exposure；不声称 DeepSeek 有对应 MCP 实现 |
| SchemaValidator、工具调用身份与 PluginAPI.event_sink | Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 `packages/ai/src/utils/validation.ts::validateToolArguments`、`packages/agent/src/agent-loop.ts::prepareToolCall` | 前置能力契约、显式身份、隔离观察和释放证据 | Python 提供可替换校验端口与离线 Draft 2020-12；不复制 TypeBox 参数转换或进程级 WeakMap 缓存。run/turn 是工具 context 的显式快照；插件借用观察端口，无 Store/Runtime 权限 |
| `runtime/middleware.py`、ToolResultHook | Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`，`packages/coding-agent/src/core/extensions/types.ts::BeforeProviderRequestEvent`、`ToolResultEventResult`、`ExtensionAPI.on`；`packages/agent/src/agent-loop.ts::ToolCallHooks` | 按阶段的能力接缝、显式注册与执行证据 | Python 使用完整值顺序链；请求不 durable，工具结果 durable；禁止翻转 is_error。没有直接对应的 Middleware 值对象，不复制 Pi 可更改 isError/usage 的结果协议或其全部拦截阶段 |
| `tools/decorator.py` 的 tool / DecoratedTool | `packages/coding-agent/src/core/extensions/types.ts::ToolDefinition`、`packages/agent/src/types.ts::AgentTool`，commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` | 显式能力值、Consumer 与执行证据 | Pi 显式提供 TypeBox parameters；Python async 签名推导无直接对应。不引入全局注册、同步线程执行或完整 schema 框架 |
| `application/` 与 `runtime/components.py`、`runtime/session_spec.py` | coding-agent 的组装与会话配置职责 | 显式配置、能力工厂和资源所有权接缝 | 应用层拥有配置、Catalog、Builder、Runtime facade 和 Lifecycle；运行时保留共同消费的组件/Session 规格。仅迁移定义位置，不改变取消和回滚语义 |
| `storage/`、`context/`、`observability/`、`tools/` | Pi 会话、压缩、观察与工具执行的职责 | 按能力组织实现与消费接缝 | 单一 Python 分发包内归组功能模块；工具、Shell 与进程隔离的机制映射见下表，端口仍由 ports 定义 |
| `runtime/loop.py::Agent.run()` | `packages/agent/src/agent-loop.ts::agentLoop()` | Runtime 消费已注册能力 | 通过显式接线提交消息、接收 steer 和调用 Hook，执行顺序工具循环 |
| `runtime/session.py`、`runtime/control.py`、`runtime/compaction_orchestration.py` | `packages/coding-agent/src/core/agent-session.ts::AgentSession` 的会话生命周期、控制与压缩 | 独立组织运行编排与能力接缝 | Python Session 组合 FIFO 控制与压缩，并通过同一个 Store 提交消息和控制转移 |
| `run_failed.error_category` | `packages/agent/src/agent-loop.ts` 的运行失败终态 | 按 Provider / Consumer 能力边界定位故障 | Python 调用方收到领域异常和原始 cause，观察者收到异常类名和稳定分类 |
| `ModelProvider` | `pi-ai` 的统一模型/流抽象 | Definition / Provider / Consumer capability seam | 每次调用显式携带 ProviderRequestContext；原始 Provider 与 resilience 组合层满足同一端口 |
| `ProviderRequestContext` | coding-agent provider hooks 的 session/model 调用上下文 | invocation-scoped identity seam | 强类型 request/attempt/purpose/session/run/turn，不含凭据或 payload；retry/fallback 保持 request ID 并为每次物理调用更新 attempt identity |
| `providers/runtime.py` 中的 `DirectModelRuntime` / `ResilientModelRuntime` 与三个策略端口 | Pi 的 model registry/runtime 与 provider selection | 显式 capability composition + invocation evidence | direct 保留一次调用的原始错误；resilient 实现能力路由、pre-output retry、backoff、兼容 fallback 和 attempt trace；主动省略动态 registry、circuit breaker 与健康/成本评分 |
| `_openai_compat` + `DeepSeekProvider` / `KimiProvider` / `GLMProvider` | `packages/ai/src/api/openai-completions.ts` 被多个 provider descriptor 复用 | 共享协议机制 + 独立 Provider policy/lifecycle | 共享消息、SSE、usage 和 tool-call fold；认证、错误分类、thinking 能力与完成判据仍留在具体适配器，retry/fallback 位于其上层组合而非 wire codec |
| `TimiccResponsesProvider` + `_sse` | Pi 的 Responses/stream 抽象与 provider-specific assistant state | 同一领域端口下的独立 wire Provider | 解析 typed SSE、output item、扁平 tools 与 Responses usage；不与 Chat Completions 共用消息/tool codec，尚无 server-side conversation |
| `AppConfigSpec` / `LoadedAppConfig` | `ModelRuntime` 的模型目录、环境认证与 runtime override | config definition + credential resolution seam | 显式 YAML 路径、override → process env → dotenv；当前覆盖 DeepSeek/Kimi/GLM/TiMi API key，不复制 OAuth、用户级 auth.json 或动态 provider registry |
| `AgentProfile` / `AgentRuntimeBuilder` / `ProviderCatalog` | coding-agent 的 `createAgentSession()` 组装边界与 model registry | explicit composition + transactional lifecycle | Profile 只含默认值/factory，Catalog 显式传入，Builder 失败逆序回滚；不使用全局 registry，尚无 CLI override 与动态发现 |
| `ProviderRegistration.parse_settings` | `packages/coding-agent/src/core/model-registry.ts::ModelRegistry.registerProvider()`，固定 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` | Definition/Provider/Consumer 的配置接缝和显式组装 | Python 注册完整 parser/factory 对，YAML 字段在边界转为类型化配置；Catalog 是应用拥有的不可变值，当前凭据采用单个 api_key_env 引用 |
| `CapabilityRegistration` / `CapabilityCatalog` | `packages/coding-agent/src/core/extensions/types.ts::ExtensionAPI.registerTool()`，固定 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` | capability 的配置定义、初始化与执行证据 | Python 用 YAML 选择注册名和类型化 Settings，通过既有 PluginAPI 贡献工具、Hook 和 Session 插件；Host 管理事务及 cleanup，当前采用显式应用目录 |
| `AgentComponents` | `packages/coding-agent/src/core/agent-session-services.ts::CreateAgentSessionServicesOptions` 与 `packages/agent/src/agent.ts::AgentOptions` | 显式组装后交给能力 Consumer | Python 解析后的模型行为组件以一个冻结对象传递，Runtime 单独管理 Store、事件和资源生命周期 |
| `SessionSpecResolver` / `SessionOpenRequest` | `packages/coding-agent/src/core/sdk.ts::CreateAgentSessionOptions`、`createAgentSession()`，固定 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` | 配置解析、能力选择与生命周期由显式接口衔接 | Python 每次打开先解析完整规格，池内 controller 固定能力实例和观察 metadata，应用提供授权规则与资源管理 |
| `pi_agent_py.api` | `packages/coding-agent/src/index.ts` 的 SDK 导出入口 | 对外发布组装入口及能力契约 | Python 用显式 `__all__` 声明稳定导入面，导出对象与定义模块共享身份 |
| `AgentRuntime` / `RuntimeSession` | `AgentSessionRuntime` 与应用侧 session 管理 | stable facade + resource ownership + session lease | 复用 AgentSession controller、惰性 TTL/LRU 驱逐、统一关闭 owned 资源；主动省略多租户 namespace 和 Web transport |
| `Message.thinking` / `Message.provider_state` | assistant `ThinkingContent` / `thinkingSignature` | model-visible provider continuity state | 可读 thinking 与 provider-owned item 分离持久化；TiMi `store:false` 原样回放 encrypted reasoning/output item，观察事件不暴露，尚无跨模型转换 |
| `ToolDefinition` / `Tool` | `AgentTool`、参数 schema 与 execute context | Capability Definition / Provider / Consumer | 使用教学型 JSON Schema 子集；handler 接收隔离快照 context |
| `ToolExecutionContext` | core toolCallId/params/signal + coding-agent `ExtensionToolContext` / `BeforeToolCallContext` | invocation-scoped capability context | 已含 call/definition、session、request/call index 与审批决定；待 signal API、cwd、identity、nested calls 和 progress |
| `ApprovalPolicy` | `beforeToolCall` block hook | 独立策略 capability | 使用完整 context，异常/错误返回值 fail-closed；默认拒绝受保护工具，尚无身份策略与 durable approval |
| `ToolExecutionGate` / `DeclaredSideEffectGate` | coding-agent `beforeToolCall`/工具组装的应用侧策略 | policy consumer seam | runtime 只经 Gate 调度；默认只允许 none effect。名称明确不承诺 OS 隔离 |
| `ShellExecutor` / `BashTool` / `SandboxedBashExecutor` | Pi Bash tool 的模型 schema 与执行 context | `dsh-shell` Definition、`tool-bash` Consumer、request/spec split | 已有模型安全参数投影、Fake 与 Docker-backed Provider、workspace/timeout/argv 边界，以及 YAML + coding Profile 显式组装；待输出 spill 和单次升级 |
| `ProcessSandbox` / `DockerProcessSandbox` | Pi 建议的外部容器/VM 隔离边界 | DeepSeek Harness subprocess confinement、per-call policy、fail-closed 与 enforcement completeness | 当前只提供 exact-argv Docker workspace Provider，并以每流有界尾部报告截断；比 same-world runner 隔离更完整，但尚无 native backend、文件工具统一路由和单次升级 |
| `SandboxedShellTool` | Pi bash 工具经容器/VM 路由的 consumer 模式 | confined shell consumer | 作为迁移期旧 Consumer，显式生成 `/bin/sh -lc` 并进入只挂载 workspace 的容器；新应用改用 `BashTool -> SandboxedBashExecutor`，未知 Python 插件仍在 Harness 进程中运行 |
| tool message | `ToolResultMessage` | model-visible durable state | 已通过 SessionStore 逐消息提交并可从 durable event replay；重启时会保守补齐尾部未决调用，副作用结果标记为 unknown |
| `EventSink` / `IsolatedEventSink` | `Agent.subscribe()` / `AgentSession.subscribe()` | live events 与 durable session events 分离 | Pi core Agent 会 await listener；本项目把 EventSink 限定为观察者并自动隔离异常，拦截行为留给 Hook |
| `RedactingEventSink` / `BufferedEventSink` | UI/session listener 的应用侧消费 | telemetry 数据最小化 + lifecycle | 默认隐藏模型可见内容后再进入有界队列；best-effort、显式关闭，不替代 durable log |
| `AgentEventEnvelope` / observer span | run/turn/model/tool lifecycle event | versioned telemetry schema + trace correlation | 公共字段移出 payload；模型/工具/压缩/Hook 有 started/terminal span 与 monotonic duration |
| `EventHub` / `RunHandle` | `packages/agent/src/agent.ts::Agent.subscribe()` | live 观察与 durable replay 分离 | 本项目用独立有界队列避免等待 listener，提供按 run 过滤、结果和定向取消；省略跨进程重放 |
| `TelemetrySink` / OTel | `packages/telemetry/src/index.ts::TelemetryContext` 与显式 span context | 版本化遥测上下文和能力执行证据 | Python 从既有 envelope 投影出无正文遥测，在可选适配器关联 span；应用管理 SDK、采样和 exporter |
| `UsageLedger` / `PricingTable` | `packages/ai/src/types.ts::Usage` 的 token 与 cost | 从观察事件统计能力使用 | 价格由应用注入，Python 账本在 Runtime 内跨 run 聚合，省略实时目录、跨进程账本与账单对账 |
| `DurableEventIdentity` | session entry identity 与运行时 committed notification | observer-to-fact join | AgentSession 的 message notification 指回 durable event；standalone Agent 无 Store 时允许为空 |
| `AgentSession` | Pi `AgentSession` 的 prompt/steer/followUp/abort、状态与 compaction 编排 | Consumer 组合 Provider、存储与控制 capability | 已支持 FIFO dispatcher、durable steer/follow-up、claim lease、abort、settled/idle、恢复和压缩；JSONL 尚无跨进程文件锁 |
| `NextTurnHook` / `NextTurnContext` | `runLoop()` 的 `prepareNextTurn` + `AgentSession._compactBeforeNextAssistantResponse()` | runtime interceptor capability seam | Agent 保持单插槽；AgentSession 先执行 durable compaction，再执行应用/插件 Hook；context 显式携带 session/run/turn/request number，尚无运行中热重载 |
| `PluginFactory` / `PluginAPI` | `ExtensionFactory` / `ExtensionAPI` | 显式 capability registration | 注册 Tool、NextTurnHook、SessionPluginFactory 与 cleanup；不含 UI/命令/MCP |
| `PluginHost` / `PluginSessionLease` | extension loader + session-bound ExtensionRunner | Provider lifecycle + evidence | staging 后原子发布；每 Session 独立实例，请求 Hook 只改投影，后台 settled observer 隔离；不做目录发现与热重载 |
| plugin hook chain | ExtensionRunner 的顺序 handler dispatch | ordered interceptor pipeline | 使用 priority + 注册顺序、逐 Hook 超时和 fail-closed；Pi 的许多 handler 会记录错误后继续 |
| `Registration` | `pi.on()` 返回的 unsubscribe | reversible explicit side effect | Tool、Hook、cleanup 注册都返回幂等撤销句柄 |
| `ContextPlan` / `ContextStrategy` | `packages/agent/src/agent-loop.ts` 的 `runLoop()` 在模型调用前执行 `transformContext` | Definition / Provider / Consumer capability seam | 已分离 durable history 与请求投影，当前默认仍发送完整历史 |
| `TokenEstimator` / `TokenBudgetContextStrategy` | `packages/coding-agent/src/core/compaction/compaction.ts` 的 `estimateTokens()` / `findCutPoint()` | 可替换估算 Provider + 策略 Consumer | 同样使用字符启发式；本项目按完整 user turn 裁剪，不允许 split turn |
| `context_prepared` | compaction/context 生命周期事件 | capability evidence | 仅记录数量与预算元数据，不持久化提示词正文 |
| `CompactionStrategy` / `ModelCompactionStrategy` | `compaction.ts` 的 `compact()` 与 summary generation | 可替换 Definition / Provider / Consumer | 使用独立 ModelProvider，不暴露工具；待增量摘要与质量 eval |
| `CompactionPolicy` / `TokenThresholdCompactionPolicy` | Pi threshold/overflow check / OpenAI `compact_threshold` | 触发 Definition + runtime Consumer + trace Evidence | run 前估算 persisted history + incoming prompt，并在每个工具续轮前重评；尚无 overflow recovery |
| `SessionCompaction` / `context_compacted` | `CompactionEntry` 与 session context reconstruction | durable capability evidence | 保存前缀替换操作并由 replay 应用；当前不支持 split-turn 和文件跟踪 |
| `SessionStore` / `SessionSnapshot` | `SessionManager` 与 session entries | Definition / Provider / Consumer / Evidence | 内存与单写者 JSONL Provider 均支持持久化 fork 和尾部未决工具调用恢复，待 durable tool-started 事实与多进程事务 |
| `prepare_session_append` / `prepare_session_compaction` / `fork_session_events` | session append / compaction / fork rules | Provider 共享的纯状态转换 | 内存和 JSONL 使用同一套 replay、append、compact 与 fork 规则 |
| `JsonlSessionStore` | Pi 的 JSONL session persistence | 可替换持久化 Provider | schema v6 支持压缩、控制 transition、event/run/turn/provenance、provider-owned state、严格格式、刷盘和 v1-v5 兼容读取，尚无跨进程文件锁与迁移工具 |
| `DurableSessionEvent` | session entries + run/turn lifecycle correlation | append-only facts + replayable projection | 每个事实有稳定 event ID、run/turn 和来源；fork 通过 copied_from_event_id 保留来源 |
| committed `Message` | agent state messages | model-visible state 必须可从 durable log 重建 | 文本、tool call、tool result 与 assistant thinking 已能回放；损坏序号会被拒绝 |
| `run_settled` / `AgentSession.wait_until_idle()` | Pi `agent_settled` / `waitForIdle()` | per-run lifecycle + session control barrier | 明确区分单 run settled 与无活动/无 follow-up 的 session idle |
| `FakeProvider` | Pi 的 faux provider/testing patterns | 以证据验证 capability 契约 | 已覆盖文本与完整 ToolCall |

## 学习原则

- 学 Pi：保持 Agent Loop 小，统一供应商事件，明确 session 生命周期。
- 学 DeepSeek Harness：能力边界由 Definition、Provider、Consumer、Lifecycle 和 Evidence 共同构成。
- 不照搬：早期不拆成大量包，不引入与当前教学目标无关的动态系统。


## Python 工具默认值绑定补充

对照固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 `packages/agent/src/types.ts::AgentTool` 与 `packages/agent/src/agent-loop.ts::prepareToolCall`：本项目保留参数校验后执行工具的边界。Python `@tool` 的定义期默认值验证、私有快照和逐调用副本属于本地函数适配器能力，无直接上游 API 对应；不修改 Pi、不把 MCP schema.default 当作本地绑定指令，也不扩展为通用参数改写/动态定义系统。证据见 test_tool_decorator、教程 33 与 ADR 0034。


## 工具定义快照补充

固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 `packages/agent/src/types.ts::AgentTool` 和 `packages/coding-agent/src/core/tools/tool-definition-wrapper.ts::wrapToolDefinition` 对应工具协议与适配边界。本项目在 Python ToolDefinition 增加嵌套 JSON 隔离，并在 Agent 构造时固定契约、委托原实现，避免移植动态扩展/UI 注册体系。不是照搬 Pi 的不可变性保证，也不增加定义的 durable 版本或热重载。证据见 ADR 0040、第三章、示例 41 和 test_tool_definition_snapshots。


## WorkspaceFiles/read 映射

对照固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 `packages/coding-agent/src/core/tools/read.ts::ReadOperations/createReadToolDefinition` 与 `truncate.ts::truncateHead`：借鉴模型工具与底层操作分离、offset/limit、行数与字节双预算及续读提示。本项目在 tools/workspace/ 集中文件能力，通过 WorkspaceFiles 和只读 ProcessSandbox 执行，拒绝绝对路径、全部符号链接及非普通文件；不移植图片缩放、平台文件名模糊匹配或 TUI。DeepSeek Harness 对应 Definition/Provider/Consumer/Lifecycle/Evidence 能力接缝，当前不拆分发布包。见 ADR 0041、示例 42、第 39 章与 test_workspace_read。


## Workspace 修改/查询映射

固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`：`packages/coding-agent/src/core/tools/write.ts::WriteOperations/createWriteToolDefinition`、`edit.ts::EditOperations/createEditToolDefinition` 对应工具与底层操作分离；`file-mutation-queue.ts::withFileMutationQueue` 对应共享修改队列与取消后等待。采用实例锁代替进程全局 Map；增加版本检查、临时原子发布与 unknown/清理语义，edit 首版只做单一精确替换，不移植模型参数兼容 shim 或 fuzzy matching。`ls.ts::createLsToolDefinition`、`find.ts::createFindToolDefinition`、`grep.ts::createGrepToolDefinition` 对应目录、glob 与文本查询；当前用标准库有界实现，不自动下载 fd/rg，不含 regex/.gitignore/TUI。DeepSeek Harness 映射仍为能力五要素，不拆包。证据：ADR 0042、第 40 章、示例 43、test_workspace_tools/test_sandbox。


## Python 契约归属与依赖检查（ADR 0043）

本次为 Python 自身的模块边界调整，不新增 Pi 行为等价承诺，也不改变上文固定上游 commit 的映射。既有模型组合和会话选择概念保持原语义；借鉴能力 Definition 与 Consumer 分离，把模型策略契约归 ports，会话选择契约归 runtime/session_spec。AST 检查是本项目为教学提供的本地证据，不声称对应某个未核实的上游机制；不引入插件热重载、独立包、动态依赖追踪或新的资源生命周期。证据为教程 41、test_architecture/test_port_contracts 与示例 23/31。
