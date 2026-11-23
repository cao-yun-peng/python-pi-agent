# Knowledge Map

| 知识点 | 理论 | 最小实现 | 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|---|
| MCP 配置（E1 阶段 4） | 纯解析、秘密引用、显式装配与目标授权 | McpCapabilitySettings、SecretResolver、mcp_capability_registration | YAML/稳定 API 完成，复用插件与 owner | 零连接预检、Catalog 双端、Fake 续轮、真实 YAML HTTP、缺 SDK 导入 | [第 38 章](../tutorials/38-mcp-configured-capability.md)、ADR 0039 | 能证明配置不自动授予启动/工具权限，秘密不入 Settings/log |
| MCP HTTP（E1 阶段 3） | 原始接收与解析后预算、HTTP 所有权和凭据边界 | 受限 httpx2 transport、SDK Streamable HTTP | HTTP 已完成；YAML/凭据引用后续 | 字节流停止、401/断流/超限、modern/legacy、无重发和正常关闭 | [第 37 章](../tutorials/37-mcp-sdk-transports.md)、ADR 0038 | 能证明解析前拒绝，并区分 unknown 与资源清理失败 |
| MCP SDK owner/stdio（E1 阶段 2） | 同任务上下文、有界队列、取消隔离、真实进程释放 | OwnedMcpEndpoint、SdkMcpConnector、分页 DTO 边界 | stdio 已实现；HTTP/wire 预算后续 | owner/SDK 脚本测试；显式 modern/legacy、远端取消、断连、Windows 树退出 | [第 37 章](../tutorials/37-mcp-sdk-transports.md)、ADR 0037 | 能证明跨任务关闭不泄漏且失败不重发 |
| MCP Fake 切片（E1 阶段 1） | 发现映射、连接授权、一次调用、结果投影与事务释放 | SDK 无关 DTO/端口、mcp_plugin、McpTool、FakeMcpConnector | 离线端到端完成；真实 SDK/传输、队列和 YAML 后续 | 权限零调用、输入/输出 schema、预算、回滚、取消恢复、并发隔离、JSONL/B5、OTel | [第 36 章](../tutorials/36-mcp-fake-tools.md) | 能追踪发现到 durable 续轮，证明失败不重发且构建失败关闭连接 |
| 通用远端工具接缝（E1 阶段 0） | 校验端口、观察借用、显式身份、未知结果 | SchemaValidator、Subset/JsonSchemaValidator、PluginAPI.event_sink、context run/turn | 通用接缝完成；MCP Fake 发现见阶段 1，真实传输待后续 | 完整 schema、离线引用、审批前顺序、快照、替换、可选依赖、worker 身份、超时/取消 | [第 35 章](../tutorials/35-generic-tool-adapter-seams.md) | 能替换校验器并证明观察和取消不改变安全边界 |
| Middleware 链（B5） | 顺序变换、投影与 durable 结果、两种失败策略 | Middleware、ProviderRequestHook、ToolResultHook、ToolResultContext | standalone/Session/Builder/Profile/Plugin；TruncateToolResults | 排序、深拷贝、失败、超时、取消恢复、插件回滚、OTel 与脱敏 | [第 34 章](../tutorials/34-middleware.md) | 能证明检索不进 replay、截断结果持久化，失败不泄漏原文且不诱导重试 |
| 类型化工具声明（B6） | 签名与 schema 同源、显式组装 | DecoratedTool、tool、ToolError；Optional 列表校验 | 装饰器与可插拔校验器完成；复杂类型推导待后续 | schema 映射、拒绝、返回值、审批、Gate、超时、取消 | [第 33 章](../tutorials/33-tool-decorator.md) | 能从签名推导模型契约并证明普通异常文本不泄漏 |
| 按功能组织模块 | 稳定入口、定义位置和显式依赖 | application、storage、context、observability、tools 目录及 providers/runtime、runtime 规格和句柄 | 应用组装、资源管理与配置归组；工具按职责拆分；迁移后删除原具体模块 | API/根包对象身份、原模块移除与既有行为回归 | 教程路径由后续任务更新 | 能从公开 API 定位新定义，并解释配置、组装、资源关闭、请求与执行的依赖 |
| 配置驱动能力 | 配置定义、运行初始化、显式注册和资源归属 | CapabilitySettings、CapabilityRegistration、不可变 CapabilityCatalog | PluginAPI/PluginHost 承接工具、Hook、Session 插件及 cleanup；Bash 新旧配置统一消费 | src 外适配器、B3/B4 组合、参数校验、冲突、取消、逆序回滚与 fail-closed Bash | [第 32 章](../tutorials/32-configured-capabilities.md)、ADR 0031 | 能从配置定义新工具，验证初始化和关闭轨迹，并说明资源何时移交 Host |
| Provider 配置扩展 | 原始字段、类型化 Settings、凭据解析与资源工厂 | ProviderRegistration.parse_settings、开放 Settings/Config Protocol | 显式不可变 Catalog、内置/外部统一解析、Builder 生命周期 | src 外独立包、凭据优先级、非法配置、运行事件、取消与回滚 | [第 31 章](../tutorials/31-registered-provider-settings.md)、ADR 0030 | 能在下游包注册供应商，通过 YAML 启动并解释每个阶段的数据和资源所有权 |
| Session 规格解析 | caller hints、完整组件选择、controller 固定规格与观察上下文 | SessionOpenRequest、SessionSpec、StaticSessionSpecResolver | Builder resolver/factory、池外解析和锁内冲突检查、深层 metadata 快照与事件传播 | 能力隔离、同 ID 冲突、并发、取消、失败、回滚、驱逐恢复及 metadata 可见性 | [第 30 章](../tutorials/30-session-spec.md)、ADR 0028 | 能按角色选择工具和 Provider，说明能力实例为何必须复用，并验证 metadata 的观察范围 |
| Run 失败分类 | 调用边界、异常链、取消和 run 终态 | Provider、SessionStore 与 runtime 分别分类 | 已覆盖入队后准备、模型流、提交和控制调度失败，事件携带稳定分类 | 原生 Store/Provider 异常、协议、内部错误、取消和 dispatcher 恢复 | [第 13 章](../tutorials/13-safe-event-observation.md#8-run-失败分类) | 能从 live 事件定位故障子系统，并从异常链取得原始诊断 |
| Agent 组装规格 | 默认 factory、解析后配置、资源所有权 | `AgentProfile.build_components()` 与冻结的 `AgentComponents`；`Agent`/`AgentSession`/`AgentRuntime` 只接收组件对象 | 已贯通 Builder → Runtime → Session → Agent | 全字段实际消费、覆盖跳过原 factory、配置验证、旧参数与接线关键字被拒绝、prompt 与初始历史冲突、插件和压缩路径 | [第 25 章](../tutorials/25-application-runtime-assembly.md) | 能独立构造组件对象、解释哪些字段影响模型行为，并用 FakeProvider 证明下游实际使用 |
| 稳定应用入口 | 公开导入面、对象身份与扩展契约 | `pi_agent_py.api` 和显式 `__all__` | 覆盖组装、主要能力端口和配套事件及错误类型 | 导出清单、旧入口对象身份、下游 Provider 和插件端到端测试 | [第 25 章](../tutorials/25-application-runtime-assembly.md) | 能仅从稳定入口导入运行时类型，接入 Provider、Tool、Store 和 EventSink |
| 运行时职责 | 执行循环、Session 生命周期、控制队列与压缩编排 | `runtime/` 四个职责模块和共享存储边界 | Session 显式组装 run，控制模块统一维护队列与 idle，压缩与 loop 经 Hook 衔接 | 旧导入身份、FIFO、失败终态、取消、durable 恢复及自动压缩回归 | [第 25 章](../tutorials/25-application-runtime-assembly.md) | 能定位一次 run 的入队、恢复、请求、提交和终态，并指出每个状态由谁拥有 |
| 消息模型 | 已覆盖 | 已完成文本、tool call、assistant thinking 与 provider-owned opaque state | 待扩展多模态和跨模型 state 转换 | 已完成 runtime/JSONL v6 回放与观察面不泄漏测试 | 第 1、2、23 章 | 能解释为何只提交完整 assistant message，以及 reasoning item 为何必须持久化但不能进入普通日志 |
| Provider 端口 | 已覆盖统一流、请求身份、purpose、attempt 与协议/供应商策略分层 | 已完成 ProviderRequestContext、Chat Completions codec 与通用 SSE framing | DeepSeek、Kimi、GLM、TiMi Responses、compaction 与 resilience 组合层已接入 | 已覆盖 standalone/session/compaction、四供应商 MockTransport、超时与取消 | 第 1、2、18、22、23、24 章 | 能独立写适配器，判断两种 API 只应共享 framing 还是可以共享完整 codec |
| Provider resilience | 已覆盖错误分类、幂等边界、总预算、路由兼容性与逻辑/物理请求身份 | 已完成 RetryPolicy、BackoffStrategy、RoutePolicy、ProviderCandidate 与 attempt trace | 已完成 pre-output retry、full jitter/Retry-After、capability/state-owner fallback；待 circuit breaker、健康/成本加权和 partial reset 协议 | 已覆盖 retry/fallback、partial output fail-closed、认证失败、opaque state、tool capability、预算、取消、脱敏与单次 settled | 第 24 章、ADR 0023 | 能预测每类失败会 retry、fallback 还是 fail，并用 request/attempt trace 证明没有重复提交模型可见状态 |
| 流式事件 | 已覆盖 | 已完成 | 已有 text/thinking/usage/tool call | 已完成 | 第 1、2、3 章 | 能区分 live、capability event 与 durable state |
| OpenAI-compatible Provider 适配 | 已覆盖 Chat Completions 与 Responses | 已完成共享 SSE framing、Chat Completions codec，以及 TiMi typed event/output item codec | retry/fallback 已由上层组合；wire 层待 server-side conversation 与更多停止原因 | 已完成四供应商 MockTransport 回归 | 第 2、3、22、23、24 章 | 能解释 function call、usage、完成事件和 reasoning continuity 在两种 API 中为何不可强行统一为同一 wire codec |
| Provider 配置与凭据 | 已覆盖配置/秘密分离、显式路径、优先级、原子写入和脱敏诊断 | 已完成强类型 YAML spec、override/process env/dotenv 解析及安全 writer | 待 CLI override schema、外部 secret store、OAuth 与多租户 credential port | 已覆盖三 Provider、优先级、无父目录搜索、未知/重复字段、缺失密钥、覆盖保护和 secret-safe repr | 第 21、22 章 | 能在 app 边界解析一次配置，证明密钥不进入 YAML、全局环境、诊断或提交历史 |
| Agent Loop | 已覆盖 | 已完成 | 已支持顺序工具循环、会话提交和 next-turn 拦截接缝，待并行 | 已完成 | 第 1、3、4、11 章 | 能画出文本、工具、会话恢复及工具续轮前 Hook 路径 |
| 取消传播 | 已覆盖 | 已完成 | 已传播到 Provider 与工具 | 已完成 | 第 1、3 章 | 能证明工具取消后 run 一定 settled |
| 工具系统 | 已覆盖 schema、上下文快照、审批、声明式副作用 Gate、超时与取消 | 已完成 ToolExecutionContext → ApprovalPolicy → ToolExecutionGate → Tool 管线 | 待身份化 RBAC/ABAC、并行、完整 schema 与 durable approval | 已覆盖成功、拒绝、策略失败关闭、默认 Gate 拒绝、显式 allowlist、参数快照隔离、超时和取消 | 第 3、14 章 | 能实现 validate/approve/gate/execute 管线，并说明 Gate 为何不构成系统隔离 |
| Bash 执行接缝 | 已覆盖 Consumer、request/spec、命令结果与基础设施失败分离 | 已完成 ShellExecutor Definition、BashTool Consumer 与 Fake Provider 示例 | 已完成 SandboxedBashExecutor、workspace 路径规范化、超时封顶、固定 Bash argv、YAML/Builder 组装与 coding Profile；待完整输出 spill 与单次权限升级 | 已覆盖安全 schema、配置验证、resolve/execute、非零退出、超时、每流截断、沙箱事实、partial enforcement、网络副作用声明、取消和 Runtime 工具管线 | 第 26、27、28 章，ADR 0025、0026、0027 | 能从 YAML 追到 Docker argv，并区分命令失败、Provider 失败与 run 取消 |
| Workspace 进程沙箱 | 已覆盖控制面/执行面、fail-closed、精确 argv、workspace mount、enforcement evidence 与清理责任 | 已完成 ProcessSandbox、Docker Provider 和兼容期 SandboxedShellTool | 已接入 ShellExecutor；待文件 read/search/patch 统一端口、单次权限升级及 OS-native/远程 Provider | 已覆盖 argv/命令构造、成功、runtime 失败、每流有界尾部、超时、取消、partial enforcement 拒绝，以及显式 Docker 写入/只读/逃逸集成测试 | 第 20、27 章，ADR 0020、0026 | 能证明 Shell 不回退宿主执行，并解释何时必须隔离整个 Harness |
| 会话与回放 | 已覆盖会话、纯状态转换、日志序号、event/run/turn/provenance、格式版本、控制队列、恢复、分叉、替换事件和未决工具调用 | 已完成共享 transition、schema v6 内存/JSONL 日志、assistant thinking/provider state replay、跨进程恢复、steer/follow-up enqueue/claim/deliver/cancel、尾部未决工具结果修复、fork 与 compaction replay | 待 durable tool-started 事实、多进程文件锁和数据库 Provider | 已覆盖纯函数、仅 v6 解码、非当前格式拒绝、必需字段校验、metadata/control round-trip、恢复、未决工具批次、fork、compaction、损坏日志、I/O、并发冲突、失败与取消 | 第 4、5、6、7、10、15、17、23 章 | 能解释 transition 与 Provider 的边界，用 run/turn/provenance 追踪 durable facts，并从日志恢复控制意图和模型上下文 |
| Session 控制面 | 已覆盖 prompt/steer/follow-up/abort、run settled 与 session idle | 已完成 FIFO dispatcher、durable control transition、claim lease、安全边界 steer 和 idle barrier | 待 retry queue、图片输入与跨进程文件锁 | 已覆盖 abort、FIFO/reopen follow-up、工具中 steer、idle 拒绝和 durable cancel | 第 16、17 章 | 能区分 steer/follow-up/abort，并从日志证明控制意图只投递给目标 run |
| 上下文策略 | 已覆盖 durable history、请求投影、预算和完整 turn 边界 | 已完成 ContextPlan、异步策略、全历史/预算实现、TokenEstimator 和 capability event | 待精确 tokenizer、资源加载和 provider-aware budget | 已覆盖投影隔离、连续后缀、工具 turn 原子性、预算失败、取消与 AgentSession | 第 8、9 章 | 能替换估算器与选择策略，且不修改 durable history 或拆断工具链 |
| 上下文压缩 | 已覆盖临时投影、完整 turn 边界、触发/摘要端口、追加式替换和乐观并发 | 已完成手动/阈值自动 durable compaction、工具续轮前触发、模型/假摘要策略、Provider purpose、JSONL v5 与 replay | 待增量摘要、精确 tokenizer、overflow 恢复和质量 eval 实现 | 已覆盖阈值决策、跳过、选择、模型协议、request context、Store 重启、失败与取消原子性 | 第 10、11、18 章 | 能分离 when/how/persist/replay，并解释为何 Hook 位于 tool result 与下一次请求之间 |
| 插件系统 | 已覆盖 factory/API/host、Session lease、原子注册、顺序、超时、观察/拦截与释放 | 已完成 Tool + NextTurnHook + SessionPlugin request projection/settled observer + cleanup | 待 durable control port、资源发现与安全热重载 | 已覆盖双 Session 隔离、投影不污染 replay、顺序、冲突回滚、超时、取消、活动 lease 卸载保护与逆序释放 | 第 12、19 章 | 能实现插件注册与 Session 实例生命周期，解释请求拦截/后台观察错误语义，并证明关闭不会遗留监听任务或跨 Session 状态 |
| 应用组装与生命周期 | 已覆盖 facade、Builder/Profile、显式 Catalog、资源所有权、Session lease、容量与关闭顺序 | 已完成 AgentRuntime/RuntimeSession、单次 Builder、代码 Profile、owned/borrowed scope 和内存 Session 池 | 已接入配置驱动 Docker Bash；待正式 CLI、多租户 SessionRef 与 Web/SSE | 已覆盖配置驱动 resilience/Bash、复用/持久历史、容量拒绝与驱逐、构造回滚、逆序关闭、取消和幂等关闭 | 第 25、28 章，ADR 0024、0027 | 能从 Profile 与显式依赖组装 Runtime，说明每个资源由谁关闭，并证明 transport 不需要拥有 AgentSession |
| Run 订阅、遥测和用量 | 有界背压、定向取消、数据投影、低基数、用量估算 | EventHub、RunHandle、LoggingTelemetrySink、UsageLedger | 可选 OTel span/指标，独立应用管理 SDK；账本保留 Runtime 内的 Session 汇总；错误投影、投递健康与外部关联见[补充设计](design/observability-evolution.md)，尚未实现 | FakeProvider 并发隔离、慢订阅、关闭/取消、失败计量；内存 span exporter 与指标读取 | [第 29 章](../tutorials/29-run-observation.md)、ADR 0029 | 能独立消费 run 与结果，验证日志不含正文，并说明 session 用量何时不完整 |
| 安全事件观察 | 已覆盖观察/拦截分离、失败隔离、数据最小化、统一 envelope、span、背压与生命周期 | 已完成 versioned AgentEventEnvelope、固定 JSON record、Isolated/Redacting/Buffered EventSink、model/tool/compaction/Hook span 及 durable event link | 待审计型 durable sink 与跨线程压力测试 | 已覆盖 envelope 校验/序列化、Agent/Session/Plugin 隔离、started/terminal 配对、duration、durable identity join、脱敏不变性、顺序投递、下游失败与关闭后丢弃 | 第 13 章、ADR 0021 | 能从 envelope 还原一次 run 的模型/工具/压缩/Hook 时间线，并解释 observer span 为何仍不能承担恢复事实 |
| 评测 | 已完成 compaction 的结构、probe QA、模型裁判和 trace 分层方案 | 待实现离线 eval case 与 deterministic grader | 待数据集、真实模型 judge、持续评测与报告版本化 | 当前只有 runtime 契约测试 | `docs/compaction-evaluation.md` | 能用版本化数据比较摘要 prompt/模型/策略，并让 judge 与人工标签校准 |


## 工具默认值契约补充

| 知识点 | 理论 | 最小实现 / 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|
| Python 工具默认值校验与隔离 | 类型注解不自动验证默认值，可变默认对象会复用 | tools/decorator.py：定义期 schema 校验、私有快照、逐调用深拷贝与转换 | test_tool_decorator：错误默认值、嵌套隔离、并发、失败、取消 | 第 33 章 / 示例 34 / ADR 0034 | 能解释省略与显式值、schema 标注与执行快照、adapter 与原函数调用的区别 |


## 工具定义快照补充

| 知识点 | 理论 | 最小实现 / 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|
| 工具契约隔离与运行时绑定 | frozen 外壳不能冻结嵌套 JSON，多次读取定义可能漂移 | domain.ToolDefinition + runtime.loop 私有 Tool 绑定 | test_tool_definition_snapshots：嵌套隔离、定义替换、Provider、审批/Gate/超时、重名、取消 | 第三章 / 示例 41 / ADR 0040 | 能证明模型与执行使用同一契约，并解释新 Agent 生效及活资源不复制 |


## Workspace 文件读取补充

| 知识点 | 理论 | 最小实现 / 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|
| WorkspaceFiles 与 read | 路径边界、no-follow、有限读取、UTF-8 完整行分页、版本和借用生命周期 | 领域 DTO + WorkspaceFiles + tools/workspace/ 下的 ReadTool/沙箱后端；内存替身共用投影 | test_workspace_read：字节/行预算、EOF、编码、Gate/审批、取消、POSIX/Docker 集成 | 第 39 章 / 示例 42 / ADR 0041 | 能替换 backend，解释只读挂载与路径检查、页截断与完整文件版本；write/edit 与查询见第 40 章 |


## Workspace 修改与查询补充

| 知识点 | 理论 | 最小实现 / 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|
| write/edit | 版本冲突、精确唯一匹配、原子发布与未知结果 | DTO + 可选 WorkspaceMutations + 工具/双 backend；stdin、实例锁、受控临时清理 | test_workspace_tools/test_sandbox，显式 Docker 故障注入 | 第 40 章 / 示例 43 / ADR 0042 | 能区分取消、提交成功但结果丢失、重放与重试；不把实例锁当作跨进程事务 |
| ls/find/grep | 可替换读取、glob、字面量匹配、有限扫描与结果 | WorkspaceQueries + QueryLimits + 专用工具，共用规则 | 结果/扫描/深度/字节/行长、链接与文件类型 | 第 40 章 / 示例 43 / ADR 0042 | 能解释 incomplete/skipped，不把有限结果当作全量扫描；regex/.gitignore 待后续 |


## 契约归属与架构护栏（ADR 0043）

| 知识点 | 理论 | 最小实现 | 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|---|
| 契约与静态依赖 | 接口拥有输入/输出，类型依赖也是维护依赖 | ports 的模型策略契约；runtime/session_spec 的 resolver | AST 解析绝对/相对/类型分支/局部导入，精确默认实现例外；扩展四个 mypy 模块 | test_architecture、test_port_contracts，模型/会话/API 回归 | 第 41 章、ADR 0043，示例 23/31 | 能解释 ProviderCandidate 为何不进 domain；违规导入会失败；旧模块属性消失且执行与取消语义不变 |

## 模型请求持久化与离线还原

| 知识点 | 理论 | 最小实现 / 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|
| 模型请求快照 | 长期会话历史与单次实际输入分别保存；提交成功后发送；prepared 不代表 delivered | ModelRequestStore、RecordingModelProvider、内存/文件实现，独立 v1 请求记录与明确 message codec v6 | RAG/工具续轮、完整消息与定义、重试尝试、失败与取消、并发排他、损坏/预算、两个独立进程恢复 | 第 42 章、示例 44、ADR 0044 | 删除检索源并结束记录进程后，能仅凭快照重建标准 Provider 输入；解释显式设置、HTTP 请求和模型输出可复现性的区别 |


## 静态系统提示词

| 知识点 | 理论 | 最小实现 / 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|
| 提示词与工具规则 | 产品文案与运行机制分离；最终工具决定规则 | SystemPromptSpec、工具贡献、纯渲染、Builder 覆盖和 durable 复用 | test_system_prompts、SessionSpec、API、快照 v1/v2 | 第 43 章、示例 45、ADR 0045 | 能解释插件时序、无角色默认、恢复不迁移，以及 FakeProvider 的验证边界 |


## 结构化压缩模板

| 知识点 | 理论 | 最小实现 / 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|
| 任务恢复摘要 | 分开事实、计划、验证证据和未知结果 | 既有 ModelCompactionStrategy 的八章节模板与自定义替换 | test_compaction、test_system_prompts、Session 失败/取消回归 | 第 44 章、示例 46、ADR 0046 | 能追踪摘要请求到 durable checkpoint，解释无工具边界与提示词要求，以及 FakeProvider 无法证明语义质量 |


## 代码任务端到端评测

| 理论 | 最小实现 | 完整实现边界 | 测试 | 教程与掌握标准 |
|---|---|---|---|---|
| 模型+工具+Harness 的组合评测；执行完成不等于修复成功 | CodingTask、TaskEnvironment、Docker Provider、正式 Runtime Consumer、CLI 和输入/轨迹/预测证据 | 单题适配已实现；真实任务分数待固定环境与模型实测，多题汇总/持续评测待实现 | FakeProvider、固定 revision、gold 隔离、持久环境、非零退出、失败、取消、截断；显式进程集成 | 第 45 章 / 示例 47 / ADR 0047；能解释 F2P/P2P、gold baseline 和固定分母，依据 trace 定位失败 |


Windows 判分传输补充：已实现 eval.sh/patch.diff 的 UTF-8/LF 写入与作用域恢复回归；开发题 marshmallow-1359 的官方参考补丁 resolved=true、77 tests passed。仅证明环境有效，真实 Agent 成绩仍待运行。


## 串行评测与续跑契约（ADR 0048）

| 理论 | 实现 | 测试 | 教程与掌握标准 |
|---|---|---|---|
| 请求启动限流、逻辑请求/物理尝试、固定分母、状态与文件恢复 | EvaluationPolicy、SharedRequestLimiter、run_case、run_batch、OfficialGrader、正式 CLI | fake clock、429、取消、checksum/镜像变更拒绝、预算不重置、OS 锁、失败分母、grade 错误隔离；真实 Docker 限制未来 Git 对象与 stdin 恢复 | 第 46 章 / 示例 48；能验证恢复对话和工作区、不重复工具，并从固定清单报告全部结果 |

上文“多题汇总待实现”由本切片补齐串行范围；并行、分布式租约、token 硬限额和持续评测平台仍未实现。
