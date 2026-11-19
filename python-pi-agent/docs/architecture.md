# Architecture

## 当前边界

```text
domain.py <- ports.py <- runtime/ <- application/ <- apps
               ^           |             ^
               |           ├── runtime/components.py resolves model-visible configuration
               |           ├── context/strategies.py implements ContextStrategy
               |           ├── context/compaction.py implements CompactionStrategy
               |           ├── plugins.py assembles Tool / NextTurnHook
               |           ├── tools/function.py adapts Python functions
               |           ├── tools/approval.py and tools/gates.py enforce declared policy
               |           ├── tools/validation.py validates tool arguments
               |           ├── tools/shell.py consumes ShellExecutor as BashTool
               |           └── tools/sandbox.py implements ProcessSandbox
               |                         ├── application/profiles.py defines product defaults
               |                         ├── application/lifecycle.py owns explicit cleanup
               |                         └── application/builder.py builds AgentRuntime
               ├── observability/events.py implements isolation, redaction, and buffering
               ├── application/configuration.py parses explicit YAML and secret references
               ├── providers/runtime.py composes routing, retry, backoff, and fallback
               ├── storage/transitions.py provides pure state transitions
               ├── storage/memory.py implements SessionStore
               ├── storage/jsonl.py implements persistent SessionStore
               ├── testing.py implements ModelProvider
               ├── providers/_sse.py provides protocol-neutral SSE framing
               ├── providers/_openai_compat.py provides Chat Completions codecs
               ├── providers/_provider_support.py provides shared defaults and validation
               ├── providers/deepseek.py implements ModelProvider
               ├── providers/kimi.py implements ModelProvider
               ├── providers/glm.py implements ModelProvider
               └── providers/timicc_responses.py implements Responses ModelProvider
```

- `domain.py` 只包含无 I/O 的值对象。
- `ports.py` 定义核心运行时所需能力，不提供供应商实现。
- `api.py` 是下游应用和扩展的稳定导入入口。`__all__` 显式声明组装入口、能力端口、领域类型、事件与错误。它直接导出定义模块中的对象，根包为已有调用提供兼容导入。
- `runtime/middleware.py` 定义 Middleware 和两阶段顺序链：ContextStrategy → provider_request → Session 插件；执行工具 → tool_result → durable 提交。请求失败抛 MiddlewareError，结果处理失败提交固定 unknown 错误，取消继续传播；压缩摘要不经过请求链。
- `runtime/loop.py` 执行单次 Agent run，消费模型流、提交消息、执行顺序工具调用并发布 run 事件。
- `runtime/session.py` 管理单个 Session 的组件、插件 lease、历史恢复与 run 装配。它通过 `storage/_writer.py` 逐消息写入 `SessionStore`。
- `runtime/control.py` 独占 FIFO 队列、活跃任务和 idle 状态，负责 steer、follow-up、取消、durable claim 和恢复。Session 向它提供 run 执行与插件终态回调。
- `runtime/run_handle.py` 提供单个排队 run 的身份、事件流、结果等待和定向取消。结果等待者取消或关闭事件流不会自动取消 run。
- `runtime/compaction_orchestration.py` 执行手动和自动压缩，通过同一 Session writer 提交摘要。自动压缩接入 run 准备和工具续轮前的 Hook。
- `pi_agent_py.runtime` 提供 `Agent` 与 `AgentSession` 的兼容导入。run 订阅在入队前绑定，应用层通过 SessionSpec 选择每个 controller 的组件。
- `application/runtime.py` 提供长生命周期 `AgentRuntime` facade、引用计数 `RuntimeSession` lease，以及有界且可惰性驱逐的 Session controller 池。
- `application/profiles.py` 保存代码拥有的产品默认值和零参数 factory，不持有密钥、HTTP client 或其他活 I/O；`coding_profile()` 提供默认拒绝审批与 bash/read allowlist Gate，不创建 Docker 或文件 backend。
- `AgentProfile.build_components(provider)` 解析出冻结的默认 `AgentComponents`。Builder 应用显式覆盖，并组合 Provider、部署配置和插件能力。SessionSpecResolver 选择组件后交给 Session 与 Agent 消费。Store、EventSink、PluginHost 及资源所有权由应用组装层独立管理。
- `runtime/session_spec.py` 定义冻结的 SessionSpec 和默认静态 resolver，`domain.SessionOpenRequest` 表达调用方 hints。Runtime 每次打开均在池锁外调用 resolver，再在锁内比较已有规格。能力实例、prompt、上限和 metadata 固定至 controller 实际退出池。Builder 支持显式 resolver 或从完整默认组件创建的 owned factory。
- `CompactionComponents` 始终携带摘要策略，可选自动触发策略。Session 通过模块内接线连接 durable 提交、steer 和 per-session Provider Hook。`Agent`、`AgentSession`、`AgentRuntime` 只接收 `AgentComponents`，旧的分散构造参数与接线关键字已移除（ADR 0028 第 5 节）；公开 `Agent` 拒绝同时提供 `initial_messages` 与 system prompt。
- `application/lifecycle.py` 明确区分 owned/borrowed 资源，在关闭或构造回滚时逆序尝试全部 cleanup。
- `application/builder.py` 使用显式 Catalog 和单次 Builder 把配置、Profile、Provider、Store、Plugin 与 observer 事务化组装成 Runtime。
- `application/provider_catalog.py` 把每个 Provider 的纯 Settings parser、构造工厂和路由能力放在不可变 ProviderRegistration 中。配置层按名称调用注册解析器，把原始 YAML 转为开放的 ProviderSettings/ProviderRuntimeConfig 协议值，Builder 再创建活的 Provider。默认四家的解析也使用此接缝，稳定 API 直接引用 Catalog 的定义模块。
- `application/capability_catalog.py` 定义配置驱动能力的 Settings、parser 和 factory。YAML capabilities 按名称解析为 ConfiguredCapability，Builder 按顺序通过 PluginHost.load 初始化，再从 snapshot 组装工具、Hook 和 Session 插件。资源归入既有 cleanup/rollback 管线，`application/capabilities/bash.py` 拥有 Bash 的 `DockerBashSettings`、parser、字段校验和内置注册，通用配置模块不识别 Bash 字段；配置根出现顶层 `bash` 键（包括 `null`）即报错。
- `context/strategies.py` 提供全历史与 token budget 两种 `ContextStrategy`、字符估算器及完整 turn 分组逻辑。
- `context/compaction.py` 提供 token threshold policy、选择可安全替换的完整旧 turn，并通过可替换模型 Provider 生成摘要。
- `storage/transitions.py` 集中实现 replay、append 与 fork 的校验和事件生成，不执行 I/O 或修改调用者状态。
- `storage/memory.py` 提供 `InMemorySessionStore`，只负责内存事件容器。
- `storage/jsonl.py` 提供带格式版本、严格解码和刷盘语义的 `JsonlSessionStore`。
- `observability/events.py` 提供同步观察适配器、runtime 失败隔离、日志脱敏和显式关闭的有界后台投递。
- `application/configuration.py` 是应用组装边界：把显式 YAML 解析成不含密钥的强类型 spec，再按 override → process env → 指定 dotenv 解析凭据；可选 Bash 节只保存 Docker workspace、镜像、资源边界和网络开关。它不搜索父目录、不修改进程环境，并只暴露脱敏诊断。
- `providers/runtime.py` 在原始 `ModelProvider` 之上提供一次调用且保留原始错误的 `DirectModelRuntime`，以及组合 `RoutePolicy`、`RetryPolicy` 和 `BackoffStrategy` 的 `ResilientModelRuntime`。它不解析供应商 wire protocol，也不改变 Agent loop 的逻辑请求计数。
- `testing.py` 是确定性 Provider 实现，也是 Provider 契约的第一份证据。
- `providers/_sse.py` 只处理 SSE event/data framing；`providers/_openai_compat.py` 只处理 OpenAI-compatible Chat Completions 的消息/工具序列化、tool call 分片与 usage 归一化。二者都不拥有认证、URL 或模型能力。
- `providers/_provider_support.py` 集中一等 Provider 的默认模型/端点、基础配置校验和通用 HTTP 状态分类；它不读取配置文件、不拥有流式控制流，也不形成 Provider 基类。
- `providers/deepseek.py`、`providers/kimi.py` 与 `providers/glm.py` 分别隔离各自 HTTP 认证、模型参数、完成判据、错误体解析和供应商特例，并组合共享 wire codec/support；它们不是继承同一个可变大基类。GLM 适配器还负责标准 API 的 `request_id`、`tool_stream`、模型级 thinking/reasoning 约束和业务错误码映射。
- `providers/timicc_responses.py` 独立实现 Responses API 的 typed SSE、扁平 function tool、output item 与 usage 语义，只复用 SSE framing 和基础配置/HTTP 分类。它固定发送 `store: false`，将 completed output item 保存为 provider-owned opaque state，以支持 Codex/GPT/Grok reasoning/tool 续轮；不把两套 wire format 塞进 Chat Completions codec，也不引入 `BaseOpenAIProvider`。
- `tools/result_truncation.py` 按字符限制 content，标记计入预算；不改变 structured_content/is_error。
- `tools/schema.py` 提供默认子集和可选离线 Draft 2020-12 校验器。SchemaValidator 由 AgentComponents → Profile/Builder 显式组装，在审批前执行；完整实现限制 dialect/vocabulary/关键字和引用来源，错误不回显参数。
- `tools/decorator.py` 从 async 函数签名生成 DecoratedTool，显式注入 context、映射 JSON 返回与 ToolError；不承担注册、审批或执行调度。
- `tools/function.py` 提供 `FunctionTool` 和 `ToolHandler`，把异步 Python 函数适配为 Tool。
- `tools/approval.py` 提供默认拒绝和显式静态审批策略。
- `tools/gates.py` 提供声明式副作用与工具名 allowlist Gate，以及显式信任的 Gate。
- `tools/validation.py` 实现当前支持的 JSON Schema 子集校验，不执行工具或审批。
- `tools/shell.py` 提供模型侧 `BashTool` Consumer 和 `SandboxedBashExecutor` Provider；前者只投影命名后的 `command/workdir/timeout_seconds`，后者把工作目录限制在 `/workspace`、施加超时上限，并以固定 `/bin/bash -lc` argv 消费 `ProcessSandbox`。直接消费 `ProcessSandbox` 的 `SandboxedShellTool` 也位于本模块。
- `tools/sandbox.py` 提供真正的 `ProcessSandbox` Provider；首个实现通过 Docker 隔离 workspace Shell。
- `plugins.py` 提供显式、事务化的 `PluginHost`，把 Tool、Hook 与 cleanup 组合成不可变能力快照。

## 功能目录

应用配置、Catalog、Profile、Builder、Runtime facade、观察接线与资源清理集中在 `application/`，Bash 注册位于 `application/capabilities/bash.py`。`application/__init__.py` 与 `application/capabilities/__init__.py` 仅提供目录说明；导入不会隐式注册能力。组件规格位于 `runtime/components.py`，Session 规格位于 `runtime/session_spec.py`，由运行时和应用层共同消费。旧根模块已删除，稳定 API 和根包导出直接引用新定义；API 过渡逻辑仍按原契约保留，后续独立清理。

存储实现、回放转换和逐消息 writer 集中在 `storage/`。上下文投影和摘要机制集中在 `context/`。事件、订阅、遥测和用量记账集中在 `observability/`。三个子包的初始化文件仅提供目录说明，具体能力由定义模块显式导入。

工具适配、审批、执行 Gate 和参数校验分别位于 `tools/function.py`、`tools/approval.py`、`tools/gates.py` 和 `tools/validation.py`；Shell 语义与 Docker 进程隔离分别位于 `tools/shell.py` 和 `tools/sandbox.py`。`tools/__init__.py` 仅提供目录说明。`application/capabilities/bash.py` 在应用组装边界解析 Settings 并通过 PluginAPI 注册工具；工具实现只依赖领域对象和端口，运行时负责调用顺序、超时与取消传播。

模型请求的路由、重试与 attempt 观察归入 `providers/runtime.py`，消费 run 的句柄归入 `runtime/run_handle.py`。迁移后原具体模块与临时归组模块 `tools/core.py` 已删除，稳定入口 `pi_agent_py.api` 与根包公开对象直接引用新定义。路径映射列在 CHANGELOG，教程、示例和下游示范代码在后续任务调整。

## 状态模型

当前 durable event log 是会话事实来源，`SessionSnapshot.messages` 是通过 replay 得到的模型可见状态投影。用户消息在请求前提交；assistant 消息只有在收到明确的 `Finish` 后才提交。流式文本和 thinking delta 立即通过 live event 暴露；完成后的 thinking 单独保存在 `Message.thinking`。`Message.provider_state` 保存 provider-owned JSON item，用于 `store: false` 的协议续接；runtime 深拷贝并持久化它，但不把它放入 observer payload，避免 encrypted reasoning content 进入普通日志。其他 Provider 必须忽略不属于自己的 state。

每个 durable event 具有稳定的 `event_id=<session_id>:<sequence>`、可选 `run_id`、可选 `turn_id` 和强类型 `EventProvenance`。一次排队任务对应一个 run；每次模型请求及其工具结果对应 `<run_id>:turn:<request_number>`。user/model/tool/recovery/compaction 的来源在提交边界声明。fork 为目标会话重新生成 event ID，并用 `copied_from_event_id` 保留源事件关系。

每个 best-effort `AgentEvent` 使用独立的 `AgentEventEnvelope`，固定保存 schema version、event/time/source、session/run/turn、request/attempt、span、duration 和可选 `durable_event_id`；事件专有数据才进入 payload。模型请求、工具执行、compaction 与 plugin Hook 使用 started/terminal span，耗时由 monotonic clock 计算。`NextTurnContext` 把目标 run/turn 显式传入 Hook，避免 run-scoped 插件事件断链。`AgentSession` 成功提交 message 后，通知 envelope 的 `durable_event_id` 指回实际 durable fact；standalone Agent 没有 Store 时该字段为空。Envelope 只建立观察关联，不能替代 durable log。

工具调用以带 `tool_calls` 的 assistant message 持久存在，工具结果以引用 `tool_call_id` 的 tool message 回填。schema 错误、审批拒绝和执行失败也是模型可见结果；外部取消则终止整个 run。

工具安全管线固定为 resolve → validate → approval context → ApprovalPolicy → execution context → ToolExecutionGate → Tool。`ToolExecutionContext` 携带 call/definition、session id、模型请求序号、批次位置和可选审批决定。审批与执行分别从原始 committed call 建立深拷贝快照，防止 policy 或 handler 修改嵌套 JSON 后污染 transcript，或把审批参数偷换成执行参数。受保护工具在审批异常、错误返回值或无策略时 fail-closed。

runtime 只通过 `ToolExecutionGate.execute()` 调度工具，不再直接调用 `Tool.execute()`。默认 `DeclaredSideEffectGate` 只允许 `side_effects="none"`，应用必须显式允许 read/write/external，并可叠加工具名 allowlist；Gate 还会二次确认受保护调用携带批准决定。`TrustedToolExecutionGate` 只适用于已经信任的 Python Tool。两者都不构成 OS 隔离，拒绝产生 `tool_execution_denied` 和模型可见 error tool result；timeout 包围 Gate/Tool 调用，取消继续传播。

Shell 语义与进程隔离是两个 capability。`BashTool` 消费 `ShellExecutor`，调用方先把可选 request 解析成默认值和上限完整的 spec，Executor 才能执行。正式 `SandboxedBashExecutor` 把模型工作目录作为 workspace 相对 POSIX 路径解析，拒绝绝对路径、`..` 和 Windows 反斜杠，再固定生成 `("/bin/bash", "-lc", command)`；`ProcessSandbox` 本身只接收精确 argv，不再偷偷决定 Shell。兼容期 `SandboxedShellTool` 仍直接消费该端口，并显式生成 `/bin/sh -lc`。`DockerProcessSandbox` 把规范化 workspace 作为唯一宿主挂载，容器根只读、默认网络关闭、不向容器转发未显式声明的宿主环境变量，并施加 capability、CPU、内存、进程数、tmpfs 和每流输出捕获限制。stdout/stderr 超限时分别保留有界尾部并报告截断，不因日志量改变命令控制流。Docker 或镜像不可用时 fail closed，不回退到宿主 Shell；timeout/取消由 Provider 强制删除容器并终止客户端。当前只支持 `read-only` 与 `workspace-write`，不把 `danger-full-access` 伪装成沙箱模式。

应用配置与产品策略不合并。可选 `agent.yaml::capabilities.bash` 使用 `DockerBashSettings` 声明 workspace、镜像、超时、输出预算和网络；相对 workspace 以配置文件目录为基准。`AgentRuntimeBuilder.from_loaded_config()` 在构建事务中创建 Sandbox/Executor/Tool，缺省配置不注册 Bash。`coding_profile()` 允许显式注册的 bash/read 进入 Gate，并继续使用默认拒绝审批；网络开启时工具声明 `external`，不能伪装为普通文件写入。

逐消息提交允许取消或进程退出发生在一个工具批次中间。`AgentSession` 每次继续运行或手动压缩前都会校验工具调用/结果序列：若会话尾部存在未决调用，就为每个缺失结果追加一个 `is_error=True` 的恢复 tool message，明确记录“外部副作用结果未知，不得自动重试”，然后才允许追加新的用户消息。已经完成的 tool result 不会重复生成。若未决调用之后已经存在其他 user/assistant 消息，append-only 日志无法在原位置安全修补，session 会以 `SessionValidationError` 拒绝继续，而不是把错误结果追加到错误位置。恢复消息本身是普通 `message_committed` durable fact，因此内存、JSONL、fork 和 replay 使用同一语义；`tool_call_recovered` capability event 提供恢复证据。

`AgentSession` 通过 `SessionStore.load()` 恢复历史，并在每条完整消息进入 Agent 内存和触发 `message_committed` 前先调用 `SessionStore.append()`。两个 Store 都调用 `prepare_session_append()` 获得不可变 `SessionAppendPlan`，其中包含待保存事件和保存后的 snapshot；Provider 只负责提交计划。序号断裂或混入其他 session 的事件会导致 `SessionReplayError`，避免从损坏日志静默恢复。`SessionSnapshot.version` 始终表示已应用 durable event 数量，不等于压缩后消息数量。

每次模型调用都显式携带不可变 `ProviderRequestContext`。普通请求以 `purpose="agent_turn"` 关联 session/run/turn/request number；压缩请求使用 `purpose="compaction"`，没有活动 run 的手动压缩允许 run/turn 为空。`request_id` 表示逻辑请求；`ResilientModelRuntime` 为每次物理调用分配独立 `attempt_id` 并递增 attempt，同一逻辑请求只占一个 Agent model-request budget。Context 不携带凭据、header 或 prompt 副本；messages/tools 仍是独立参数。

Provider resilience 是显式组合层。`CapabilityRoutePolicy` 先按 purpose、tool capability 和最近 provider-owned state 的 owner 过滤候选；`DefaultRetryPolicy` 只重试 rate limit、timeout、transport 和明确 retryable 的 HTTP 错误；`FullJitterBackoff` 支持有上限的指数 full jitter 与数字 `Retry-After`。只要某次 attempt 已交付任意流事件，就立即 fail closed，不自动 retry 或 fallback。候选耗尽后抛出只含脱敏 `AttemptSummary` 的 `ProviderExhaustedError`，真实末因保留在异常链中。route/attempt/retry/fallback 通过 capability events 观察，不成为 durable session facts。

`AgentSession` 的控制面使用显式 FIFO dispatcher，而不是把并发请求隐式阻塞在互斥锁上。公开状态为 idle/running/aborting/settled；`run()` 等待自己的排队任务，`follow_up()` 返回新 run ID 并在前序 run settled 后执行，`wait_until_idle()` 等待活动任务和 follow-up 队列都清空。`steer()` 只在活动 run 的接收窗口开放时成功，在当前模型/工具 turn 的安全边界提交为 user message后继续同一 run。最终 turn 检查空队列与关闭接收窗口共用控制锁，保证成功入队的 steer 不会因结束竞态丢失。`abort()` 默认取消活动任务、清理 steer/follow-up 并等待 idle；已提交 durable facts 不回滚。

`AgentRuntime` 位于单 Session 编排之上，不复制 Agent loop。`open_session()` 为同一 session ID 复用一个 `AgentSession` controller，并返回必须释放的 `RuntimeSession` lease；容量或 TTL 驱逐只选择无 lease、已 restore 且 idle 的 controller，durable history 仍留在 `SessionStore`。Runtime 关闭时依次拒绝新工作、在 grace period 内等待 idle、关闭所有 Session、逆序关闭 PluginHost/Provider/Store，发出 `runtime_closed` 后最后关闭 observer。Builder 构造失败使用相同所有权规则回滚，borrowed 资源永远不由 Runtime 关闭。

每次模型请求前，runtime 把完整 committed history 交给 `ContextStrategy`，并只把返回的 `ContextPlan.messages` 发送给 `ModelProvider`。默认策略不省略消息。`context_prepared` capability event 暴露源消息数、选中数和省略数，但不复制消息正文。策略投影不会删除或改写 `Agent.messages` 与 durable log。

预算策略通过可替换 `TokenEstimator` 计算投影大小。system 消息固定保留；从 user 到下一个 user 之前的 assistant tool call、tool result 与最终回答组成不可拆分 turn。策略保留连续的最近 turn，mandatory context 超预算时显式失败，不截断当前输入或工具链。

durable compaction 可由 `AgentSession.compact()` 显式触发，也可在配置 `CompactionPolicy` 后自动触发。run 前，`TokenThresholdCompactionPolicy` 估算 persisted history 加 incoming prompt；同一工具循环内，Agent 在所有 tool result 提交后、下一次模型请求前调用 `NextTurnHook`，`AgentSession` 的适配器再次估算当前 durable snapshot。达到阈值后，`prepare_compaction()` 保留 leading system messages 与指定数量的近期完整 turn，`CompactionStrategy` 只为旧前缀生成摘要。摘要成功后，Store 以乐观版本检查追加一个 `context_compacted` event；replay 将指定前缀替换为带标记的 summary message，Hook 再把 replay 后的 canonical history 返回给 Agent。旧消息事件不删除，所以失败与取消不会产生半成品，重启与 fork 仍走同一 replay 规则；compaction 仍不减少日志磁盘占用。

`SessionStore.fork()` 从源日志的指定 version 创建一个新的目标 session。两个 Store 都调用 `fork_session_events()` 复制经过 replay 校验的历史前缀，拒绝覆盖已有目标，并允许源和目标在分叉后独立追加。当前事件模型没有 `session_created` 元数据，因此持久化 fork 必须至少包含一个事件。

`JsonlSessionStore` 把多个 session 的事件写入同一个 JSONL 文件，每行包含 `schema_version` 和一个完整事件。schema v2 加入 assistant thinking，v3 加入 message/compaction 互斥 payload，v4 加入 event/run/turn/provenance，v5 加入 typed control transition，v6 加入 provider-owned opaque state；解码器仍可读取 v1-v5 日志。读取、反序列化和 replay 使用相同领域对象，因此 runtime 无需感知存储变化。append、compact、record_control 与 fork 都通过实例内锁串行执行，写入后调用 `flush()` 与 `fsync()`；它支持进程重启后的恢复和分支恢复，但尚不支持多个进程同时写入同一文件。

steer/follow-up 不再把内存 deque 当事实来源。接受控制意图先写 `control_enqueued`，dispatcher 写带 lease 的 `control_claimed`，安全边界用一个携带 user message 的 `control_delivered` 同时完成消息提交与消费；abort 或失效目标写 `control_cancelled`。Replay 由这些事实重建 `SessionSnapshot.pending_controls`。steer 绑定原 run，重启后没有活动目标就取消；follow-up 预留独立 run ID，未 claim 或 lease 已过期时由 `AgentSession.restore()` 恢复。该语义保证 durable history 中同一 control 只投递一次，但不把外部工具副作用宣传为 exactly-once。

插件系统采用显式 assembly，而不是目录扫描或 import 副作用。`PluginFactory` 通过受限 `PluginAPI` 注册工具、有序 `NextTurnHook`、session plugin factory 和 cleanup；`PluginHost.load()` 先 staging，factory 与冲突校验全部成功后再原子发布。工具重名会拒绝整个插件。Hook 按 priority 与注册顺序串行组合，带独立超时，失败关闭并保留取消传播。卸载先撤销注册，再反向执行 cleanup。`PluginCapabilities` 是组装时快照，当前不支持运行中的热重载。

`AgentSession` 第一次 restore 时通过 `PluginHost.open_session()` 获得固定的 `PluginSessionLease`。每个 factory 只接收 session ID，并为每个 Session 返回独立实例。请求 interceptor 接收 `ProviderRequestContext + ContextPlan`，只能返回新的请求投影，不能改 `source_message_count`、访问 Store 或修改 canonical history。run-settled observer 作为隔离后台任务执行，lease close 会等待观察任务并逆序释放实例。有活动 lease 时 host 拒绝单独 unload；host close 先关闭 lease。插件若未来需要发送 steer/follow-up，必须通过复用 durable control transition 的受限端口实现。

观察与拦截使用不同错误语义。`Agent`、`AgentSession` 和 `PluginHost` 自动把应用的 `EventSink` 包进 `IsolatedEventSink`；sink 异常只产生有界 `EventSinkFailure`，并保留失败事件的 envelope event ID，不改变模型、工具、持久化或插件控制流，也不会覆盖原始错误。失败诊断不保存异常消息，避免把密钥或用户数据从异常文本二次写入日志。正常同步 sink 仍保持立即、有序投递，已有 UI 和测试不需要异步化。

运行错误按调用边界分类。Provider 流的原生异常转成 `ProviderError`，SessionStore 调用的原生异常转成 `SessionStorageError`，运行编排中的未分类异常转成 `InternalError`。既有领域错误原样传播，原生异常保留在 `__cause__` 中。live `run_failed` 记录异常类名和稳定的 `error_category`，随后发出 `run_settled`。已入队的 run 在 Agent loop 之前失败时，由 Session 发出这一对事件。取消沿调用链传播并发出 `run_cancelled` 和 `run_settled`。follow-up 领取失败后，已持久化的控制意图仍可在重新打开 Session 时恢复。运行结束后的 steer 清理若写入失败，Session 发出 `session_control_cleanup_failed` 并继续调度。

日志安全由应用显式组装，而不是破坏原始事件。`RedactingEventSink` 复制 payload 并保留原 envelope identity，默认隐藏 message/text/thinking、工具参数、结构化结果和嵌套敏感键；token 用量元数据保留。`serialize_agent_event()` 把 envelope 与脱敏 payload 写成固定 JSON record。慢 I/O 可放入 `BufferedEventSink` 的有界队列，由单 worker 顺序送往同步 logger。推荐顺序是 `Isolated(runtime) -> Redacting -> Buffered -> logger`，使数据在入队前已脱敏。队列满或 logger 失败允许丢失并记录诊断，因此 EventSink 仍是 best-effort 观察通道，不能替代 `SessionStore` durable log。后台 worker 必须通过 `aclose()` 显式排空和关闭。

### Runtime 观察消费面

SessionSpec 的 metadata 保存为独立 JSON 快照，暴露在 RuntimeSession.metadata 和观察 envelope。Runtime 私有 ContextVar 随调度器、模型组合层和插件子任务传播上下文，controller sink 绑定目标 metadata 以支持外部定向取消。序列化输出 metadata，RedactingEventSink 递归处理敏感键，TelemetrySink 接收清空自由 metadata 后的投影。SessionSpec 在重启时重新解析，模型上下文仍从 SessionStore 中的消息和显式组件构造。

`AgentRuntimeBuilder` 为 Runtime、Session、插件和模型组合层提供同一个观察入口，先统计用量，再向有界 `EventHub`、应用 `EventSink` 和仅含投影字段的 `TelemetrySink` 分发。应用 sink 与遥测异常分别隔离。它们属于 Runtime 基础设施，沿用 ADR 0028 的边界，不进入 `AgentComponents` 或模型上下文。关闭时先结束 Session 和生产者，发送 `runtime_closed`，再关闭 Hub 与 owned observer。

`RuntimeSession.submit()` / `follow_up()` 在 FIFO 入队前用 run ID 绑定 `RunHandle` 订阅，分派开始时发出 `run_started`。`events()` 是有界单流；`result()` 独立等待；`abort()` 只取消目标。`UsageLedger` 从最终 `model_usage` 报告计算 Runtime 生命周期内的 Session 及近期 run 合计。durable log 仍是恢复事实来源；事件流与用量观察不在重启后复原。见 [ADR 0029](adr/0029-run-scoped-event-stream-and-telemetry-sink.md)。

## 依赖规则

- domain 不依赖 ports 或 runtime。
- ports 只依赖 domain。
- runtime 依赖 domain 和 ports，不依赖具体 Provider。
- app_runtime 依赖 runtime 与 ports，只编排会话、租约和生命周期，不解释供应商 wire protocol。
- profiles 只依赖稳定端口并保存 factory；不得持有密钥、连接、后台任务或可变进程级注册表。
- assembly 可以依赖具体 Provider/configuration，但只能在应用边界解析与创建它们，并向内组装为稳定端口。
- OpenAI-compatible codec 只依赖 domain 与稳定错误；具体 Provider 可以组合它，但 codec 不反向识别供应商配置或认证。
- Provider support 只依赖稳定错误，集中无状态默认值、校验和状态码映射；供应商响应体解析、配额特例及 stream 生命周期仍留在具体适配器。
- model_runtime 只消费 `ModelProvider` 与三个策略端口；它不依赖具体供应商类，候选及 capability/state-owner 元数据由 apps 显式组装。
- runtime 消费 `ContextStrategy` 端口；默认实现位于 context，策略不得反向修改 durable history。
- runtime 消费 `CompactionStrategy` 端口；策略只生成摘要，持久化由 `SessionStore` 控制。
- runtime 消费 `CompactionPolicy` 端口；policy 只决定何时触发，不生成摘要或写存储。
- Agent loop 消费 `NextTurnHook` 端口；Hook 可以在安全边界替换下一轮 canonical history，但 SessionStore 与 compaction 细节只存在于 `AgentSession` 适配器。
- session_transitions 只依赖领域对象和稳定错误，不依赖存储 Provider。
- session_transitions 校验 tool call/result 批次，只把尾部缺失结果暴露为可追加恢复项；非尾部缺口显式失败。
- session 与 session_jsonl 实现 ports 中的 `SessionStore`，ports 不依赖具体存储。
- apps 从 `pi_agent_py.api` 导入 `AgentRuntimeBuilder` 和 `AgentRuntime`，并显式选择 Profile、Catalog 与资源所有权。能力注册发生在组装阶段。
- 配置文件只在 apps 边界解析一次；runtime/ports/Provider 不读取 YAML 或 `.env`。YAML 只引用密钥变量名，原始密钥只进入冻结的 Provider config 且不进入 repr/诊断。
- PluginHost 只组合端口实现，Agent/runtime 不反向依赖插件容器。
- 插件拦截 Hook 失败关闭；观察型 EventSink 由 runtime 隔离失败，不得改变控制流。
- 日志适配器只能消费事件副本；脱敏不得修改模型可见消息或 durable state。
- BufferedEventSink 是可丢失的观察队列，不能承载恢复、审计或业务提交事实。
- runtime 顺序执行工具；并行策略要等资源隔离与取消语义稳定后再加入。
- ApprovalPolicy 只作决定，ToolExecutionGate 消费声明式策略；用户批准不会自动扩大 effect/tool allowlist。
- ProcessSandbox 只运行进程请求，不接收任意 Python Tool；隔离 Provider 不可用时必须 fail closed。
- ToolExecutionContext 是运行时快照而非 durable fact；跨进程人工审批需要独立持久状态机。
- steer/follow-up 已有 durable enqueue/claim/deliver/cancel；JSONL 仍缺少跨进程文件锁，因此 claim lease 目前主要提供可验证的恢复契约，而不是完整的分布式队列。

Middleware 通过 Profile factory、Builder.with_middleware 或 PluginAPI.register_middleware 显式组装。Builder 替换 Profile 项并追加插件快照；SessionSpec 按每项身份比较。资源由应用或插件 cleanup 管理，无运行中热重载。middleware span 与 tool_result_transformed 只包含身份、阶段、计数和分类，不承载恢复状态。

PluginAPI.event_sink 只读借用 Host 的隔离观察端口，插件不拥有其关闭权。ToolExecutionContext 的 run_id/turn_id 从活动 run 显式传给审批、Gate、handler 和结果 Hook，可跨 worker 任务传递；它们不是新增 durable fact。工具外层超时明确提示结果可能未知，不授权自动重试。详见 ADR 0035。

## MCP 工具适配（阶段 1）

`tools/mcp/contracts.py` 定义 SDK 无关 DTO、Connector/Endpoint 和连接策略；`plugin.py` 在 build 时先检查全部精确目标授权，再打开连接、立即登记 cleanup、发现目录并按显式原名 allowlist 预检。PluginHost 原子发布普通 McpTool；失败、冲突或取消逆序关闭。应用把同一 SchemaValidator 交给插件和 Builder，不修改 Agent loop。

McpTool 始终声明 external、要求审批且不可安全重试/重放。稳定摘要本地名映射回绑定原名，一项调用只提交一次；输出先校验原值和已解析数据预算，再经过 B5 变换进入 Store。结构化封装保留 schema 摘要、协议版本与 null presence；未知结果使用固定错误，取消交给 Session 恢复。连接与请求 capability 事件经隔离观察入口发出，OTel 显式配对，不含参数、输出、地址或凭据。

`testing.FakeMcpConnector/FakeMcpEndpoint` 验证发现至模型续轮、回滚与并发取消隔离，无 SDK/网络。SDK owner、stdio/HTTP、分页、有限队列、wire 预算、凭据解析及 YAML/facade 接线尚未实现；当前解析后预算不能替代原始读取限制。见 [ADR 0036](adr/0036-mcp-fake-tool-slice.md)。

## MCP SDK owner 与 stdio（阶段 2）

OwnedMcpEndpoint 在同一 owner 任务持有 SDK 上下文，以有界队列和请求子任务处理取消；build/run/close 可以来自不同任务。SdkMcpConnector 延迟加载固定 SDK 2.3.0，协商 modern/legacy；Session.send_request 单次提交工具操作，完整分页后固定快照。拒绝发生于发送前时结果为 not_started；协议/目录失效要求重建。stdio 精确 argv、显式 env、丢弃 stderr，owner Context 内的 SDK 日志过滤器在关闭时撤销。默认 cleanup grace 调整为 10 秒，owner 清理为 8 秒；实际 Windows 进程树退出有显式本地集成证据。HTTP 与 wire 预算仍待阶段 3，见 [ADR 0037](adr/0037-mcp-sdk-owner-and-stdio.md)。

## Streamable HTTP（阶段 3）

同一个 SDK owner 显式拥有独立 httpx2 client 与受限 transport。默认 HTTPS，loopback 明文开发另需许可；TLS 保持验证，不继承环境代理/认证。SDK 解码前限制 Content-Length、流累计字节和单 SSE event，拒绝压缩、重定向及网络恢复；解析后预算仍独立执行。预接收拒绝转成固定 SDK 错误响应，流错误转成 StreamError，再由本地 fault 恢复分类，避免污染 task group/关闭。JSON/SSE、modern/legacy 与失败后正常释放均由 loopback 集成验证，见 [ADR 0038](adr/0038-mcp-streamable-http-budgets.md)。

## YAML MCP capability（阶段 4）

application/capabilities/mcp.py 纯解析非秘密 Settings，显式注册 CapabilityCatalog。command/cwd 相对配置目录规范化，args 原样；env/header 只保存引用。应用借用 SecretResolver、共享 SchemaValidator 和精确连接策略，工厂先解析所有引用/验证头，再复用 mcp_plugin 的全部目标预检与事务。默认 Catalog 不变，无自动 SDK 导入或环境读取。稳定 api 导出 DTO/端口、SDK connector/options、Tool 和配置工厂，不暴露 SDK 类型，见 [ADR 0039](adr/0039-configured-mcp-capability.md)。


## Python 工具默认值绑定

`tools/decorator.py` 在定义时以生成的参数 schema 校验有限 JSON 默认值；失败为保留 cause 的 TypeError。DecoratedTool 保存与原函数/schema 标注分离的私有默认值快照，执行时只为省略参数复制默认值，再走已有类型转换。显式值、模型 ToolCall、审批上下文与 durable 参数不变；原函数直接调用和 MCP schema default 的语义不变。该绑定不引入新端口或主循环分支，见 ADR 0034、教程 33。


## 工具定义快照

ToolDefinition.input_schema 保存有限 JSON 的隔离 Mapping，嵌套读取值为独立副本。Agent 构造时通过私有 Tool 绑定捕获单一定义，供模型声明、校验、审批、Gate、context 和超时使用；执行委托原 handler，不复制活资源。捕获后的重名立即拒绝，更新需要新 Agent；AgentSession 继续在每个 run 的 Agent 组装点捕获契约。见 ADR 0040、第三章教程与示例 41。


## WorkspaceFiles 与 read

WorkspaceReadRequest/WorkspaceReadLimits/WorkspaceReadResult 是纯领域契约；ports.WorkspaceFiles 先提供 read_text。ReadTool 只依赖端口，testing.InMemoryWorkspaceFiles 与 tools.workspace.sandbox_backend.SandboxedWorkspaceFiles 复用有界 UTF-8 页投影，后者借用 ProcessSandbox 执行固定 argv、只读 /workspace、逐组件 no-follow 和有限读取。路径/页/文件预算及安全错误在能力边界落实，完整原字节版本和 next_offset 进入 tool result。借用服务无后台资源，插件注册由 Host 释放；无新 loop 分支或 YAML 自动注册。见 ADR 0041、第 39 章与示例 42。
