# 更新记录

## Unreleased

- 新增显式请求记录接缝：RecordingModelProvider 在调用前保存最终消息、工具定义、身份及公共设置，支持内存替身与排他发布的文件快照；写入失败或取消不调用模型。独立记录 v1 保留 Session message codec v6，已有 Session 格式不变。示例 44、第 42 章和 ADR 0044 覆盖进程退出后无检索源的离线还原。

- **具体导入破坏性变更：**ProviderCandidate、AttemptFailure、RetryDecision、RetryAction 从 providers.runtime 移至 ports；SessionSpecResolver 从 ports 移至 runtime.session_spec。旧属性移除，不留转导出；根包既有 DTO 导出和 api.SessionSpecResolver 保持同一对象。新增 AST 依赖护栏，扩展四个核心模块的 strict mypy 范围。见 ADR 0043、教程 41。

- 新增 workspace write/edit/ls/find/grep、类型化修改/查询契约及两种 backend；修改默认要求审批与显式 allow_write，新建排他发布、覆盖检查版本、精确编辑保留 BOM/未修改换行，未知结果不重试并清理自有临时文件。查询有界且不跟随链接，grep 首版为字面量、未接入 .gitignore。
- ProcessSandbox 新增隐藏 repr 的 bytes stdin，Docker 同时发送输入并捕获有界输出，解决大内容的命令行长度/诊断暴露问题。coding/read-only profiles 仅放行显式注册的相应工具，主循环不变。见 ADR 0042、教程 40、示例 43。

- workspace 文件工具归入 tools/workspace/：ReadTool 位于 read.py，SandboxedWorkspaceFiles 与文本投影位于 sandbox_backend.py。稳定 API 导入不变，Shell 与通用进程沙箱保持原路径。

- 新增 WorkspaceFiles、类型化读取 DTO/预算、ReadTool 和 SandboxedWorkspaceFiles；固定 Python helper 在只读 workspace 中拒绝符号链接/非普通文件，限制原始读取并返回有界 UTF-8 页面、版本与续页位置。InMemoryWorkspaceFiles 为离线测试替身；read_only_profile 仅放行 read，coding_profile 扩充为 bash/read。示例 42、教程 39、ADR 0041 与显式 Docker 集成覆盖完整链路。

- ToolDefinition 的 input_schema 现在保存有限 JSON 隔离快照；外部原字典或读取出的嵌套值无法修改定义。Agent 捕获并绑定单一工具定义，模型、校验、审批、Gate 与超时使用同一版本，委托原 handler 执行。自定义 Gate 依赖 Tool 协议；schema 序列化使用 dict(input_schema)。新增示例 41、快照回归测试和 ADR 0040。

- 修复 `@tool` 默认值契约：定义时拒绝与参数类型或 Literal 不符的默认值；省略参数时从私有快照逐调用深拷贝，并应用现有类型转换，避免跨调用、跨 Agent 共享列表/字典。显式值优先，原函数直接调用与 MCP 默认值语义不变。示例 34、教程 33 和 ADR 0034 同步更新。

- MCP 阶段 4：新增纯 YAML Settings、显式 SecretResolver/MappingSecretResolver、mcp_capability_registration 与稳定 API 导出。默认 Catalog 不变；全部引用和目标在首个连接前预检，复用现有插件事务。示例 40 与教程 38 完成首版教学链路，见 ADR 0039。

- MCP 阶段 3：新增独立 Streamable HTTP client、显式 headers、HTTPS/loopback 策略与 JSON/SSE 接收预算；拒绝压缩、重定向与网络恢复。失败通过 SDK 兼容通道返回 unknown，正常释放资源；本地集成验证两种协议，见 ADR 0038。

- MCP 阶段 2：可选 [mcp] 固定 SDK 2.3.0；新增独立连接 owner、有界队列、完整分页和真实 stdio。未发送失败区分 not_started；SDK 日志按 owner Context 隔离。cleanup grace 默认由 5 调整为 10 秒，为 SDK 的关闭/强杀/回收留出空间；owner 默认清理 8 秒。真实子进程集成需显式启用，见 ADR 0037。

- MCP 阶段 1：新增 SDK 无关契约、显式连接策略、mcp_plugin、McpTool 和 FakeMcpConnector/FakeMcpEndpoint。打通发现、前置校验、审批/Gate、单次远端调用、输出校验/预算、B5 变换、durable 续轮与离线恢复；覆盖事务回滚、超时/取消及 MCP OTel span。定义模块入口已可使用；SDK/stdio/HTTP、凭据解析与 YAML/稳定 facade 接线留待后续阶段，详见 ADR 0036。

- MCP 阶段 0：新增 SchemaValidator、SubsetSchemaValidator、JsonSchemaValidator 与 Builder.with_schema_validator；默认校验行为兼容，完整 Draft 2020-12 通过 [jsonschema] 可选安装，引用仅限内部/应用登记资源。PluginAPI.event_sink 只读借用隔离观察通道，ToolExecutionContext 增加可选 run_id/turn_id。工具超时文本现在明确结果可能未知、不要自动重试；取消语义不变。

- 修正未启用延迟注解时，Optional/list/Annotated 中 JSONObject 的递归别名解析，组合写法与字符串注解具有一致的 schema。

- 新增 Middleware、MiddlewareError、ToolResultContext、ToolResultHook、TruncateToolResults 稳定 API；AgentComponents.middleware 默认空 tuple，支持 Profile、Builder 与插件显式组装。请求链只改投影，结果链在 durable 提交前变换；含超时/取消、失败隔离策略、无正文事件与 OTel span。

- 新增稳定 API `tool`、`DecoratedTool`、`ToolError`，从 async 函数签名生成工具。JSON Schema `type` 列表现在真正校验，并继续检查非空 object/array 的内部值；非法 type 列表明确拒绝。复杂类型推导留待后续；可插拔 SchemaValidator 见本节阶段 0 条目。

- **破坏性变更：移除旧构造参数和兼容接口。** 理由见 ADR 0028 第 5 节与 ADR 0031。

  | 旧写法 | 新写法 |
  |---|---|
  | `Agent(provider, tools=..., system_prompt=..., ...)` | `Agent(AgentComponents(provider, tools=..., system_prompt=..., ...))` |
  | `AgentSession(id, store, provider, tools=..., ...)` | `AgentSession(id, store, AgentComponents(provider, tools=..., ...))` |
  | `AgentRuntime(provider, store, tools=..., ...)` | `AgentRuntime(AgentComponents(provider, tools=..., ...), store)` |
  | `compaction_strategy=s, compaction_policy=p` | `compaction=CompactionComponents(strategy=s, policy=p)` |
  | `Agent(..., message_committer=...)` 等接线关键字 | 已移除，仅供 `AgentSession` 内部使用 |
  | `await runtime_session.follow_up_id(prompt)` | `(await runtime_session.follow_up(prompt)).run_id` |
  | 配置根 `bash:` | `capabilities:` 下的 `bash:` |
  | `AppConfigSpec.bash`、`AppConfigSpec.configured_capabilities` | `AppConfigSpec.capabilities` |
  | `from pi_agent_py.application.configuration import DockerBashSettings` | `from pi_agent_py.application.capabilities.bash import DockerBashSettings`（根包导出不变） |

  - 首个组件参数不是 `AgentComponents` 时抛 `TypeError`，旧关键字不再接受。
  - 配置根出现 `bash` 键即抛 `ConfigurationError`，包括 `bash: null` 和空值，不会被当作“未配置”静默忽略。
  - 公开 `Agent` 同时收到 `initial_messages` 与 `components.system_prompt` 时抛 `ValueError`，与旧版行为一致，避免 system prompt 被静默丢弃。

- 存储、上下文、可观测性和工具执行按功能归组，原具体模块已删除。稳定入口 `pi_agent_py.api` 和根包导出引用新定义，具体模块导入按下表迁移。

  | 原模块 | 新模块 |
  |---|---|
  | `pi_agent_py.context` 的策略对象 | `pi_agent_py.context.strategies` |
  | `pi_agent_py.compaction` | `pi_agent_py.context.compaction` |
  | `pi_agent_py.session` | `pi_agent_py.storage.memory` |
  | `pi_agent_py.session_jsonl` | `pi_agent_py.storage.jsonl` |
  | `pi_agent_py.session_transitions` | `pi_agent_py.storage.transitions` |
  | `pi_agent_py.runtime._persistence` | `pi_agent_py.storage._writer` |
  | `pi_agent_py.events` | `pi_agent_py.observability.events` |
  | `pi_agent_py.event_hub` | `pi_agent_py.observability.event_hub` |
  | `pi_agent_py.telemetry` | `pi_agent_py.observability.telemetry` |
  | `pi_agent_py.otel` | `pi_agent_py.observability.otel` |
  | `pi_agent_py.usage` | `pi_agent_py.observability.usage` |
  | `pi_agent_py.runtime._observation` | `pi_agent_py.observability._session_events` |
  | `pi_agent_py.tools` / `pi_agent_py.tools.core` 的对象 | 按职责拆分，见下表 |
  | `pi_agent_py.shell` | `pi_agent_py.tools.shell` |
  | `pi_agent_py.sandbox` | `pi_agent_py.tools.sandbox` |
  | `pi_agent_py.model_runtime` | `pi_agent_py.providers.runtime` |
  | `pi_agent_py.run_handle` | `pi_agent_py.runtime.run_handle` |
  | `pi_agent_py.assembly` | `pi_agent_py.application.builder` |
  | `pi_agent_py.app_runtime` | `pi_agent_py.application.runtime` |
  | `pi_agent_py.lifecycle` | `pi_agent_py.application.lifecycle` |
  | `pi_agent_py.configuration` | `pi_agent_py.application.configuration` |
  | `pi_agent_py.profiles` | `pi_agent_py.application.profiles` |
  | `pi_agent_py.provider_catalog` | `pi_agent_py.application.provider_catalog` |
  | `pi_agent_py.capability_catalog` | `pi_agent_py.application.capability_catalog` |
  | `pi_agent_py._runtime_observation` | `pi_agent_py.application._observation` |
  | `pi_agent_py.configured_bash` | `pi_agent_py.application.capabilities.bash` |
  | `pi_agent_py.components` | `pi_agent_py.runtime.components` |
  | `pi_agent_py.session_spec` | `pi_agent_py.runtime.session_spec` |

  | 工具对象 | 定义模块 |
  |---|---|
  | `FunctionTool`、`ToolHandler` | `pi_agent_py.tools.function` |
  | `DenyByDefaultApprovalPolicy`、`StaticApprovalPolicy` | `pi_agent_py.tools.approval` |
  | `DeclaredSideEffectGate`、`TrustedToolExecutionGate` | `pi_agent_py.tools.gates` |
  | `validate_tool_arguments` | `pi_agent_py.tools.validation` |
  | `SandboxedShellTool` | `pi_agent_py.tools.shell` |

  教程、示例和下游示范包的具体路径在后续任务调整。

  应用层迁移只调整目录、定义引用和导入；旧构造参数、内部接线参数、follow_up_id 与顶层 bash 配置已在后续变更中移除，见本节第一条。

- 新增 CapabilityCatalog、类型化 Settings 和 capabilities YAML 节，通过既有 PluginHost 事务注册工具、Hook、Session 插件及资源 cleanup。Bash 迁入内置注册；顶层 bash 配置已移除，见本节第一条。

- ProviderRegistration 新增 parse_settings，配置加载支持显式 provider_catalog，Settings 和运行配置改用开放 Protocol。Catalog 提供返回新实例的 with_registration，内置四家与外部 Provider 通过统一入口解析，原有 assembly 导入保持对象身份。

- 新增 SessionOpenRequest、SessionSpec 和 SessionSpecResolver，支持每次打开时解析完整组件和观察 metadata。Builder 支持实例注册与基于默认组件的 resolver factory，同 ID 的冲突规格会被拒绝。metadata 作为 envelope 的 JSON 字段序列化并参与日志脱敏。

- `RuntimeSession.submit()` 和 `RuntimeSession.follow_up()` 返回 `RunHandle`，提供 run 事件流、独立结果等待和定向取消。原先使用 `follow_up()` 返回字符串的调用改用句柄的 `run_id`。底层 `AgentSession.follow_up()` 的签名不变。
- 新增 Runtime 事件订阅、结构化日志及可选 OpenTelemetry 遥测适配器，以及 run/Session 用量汇总和可注入的估算价格表。
