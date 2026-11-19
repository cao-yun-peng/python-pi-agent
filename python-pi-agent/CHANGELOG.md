# 更新记录

## Unreleased

- 修正未启用延迟注解时，Optional/list/Annotated 中 JSONObject 的递归别名解析，组合写法与字符串注解具有一致的 schema。

- 新增 Middleware、MiddlewareError、ToolResultContext、ToolResultHook、TruncateToolResults 稳定 API；AgentComponents.middleware 默认空 tuple，支持 Profile、Builder 与插件显式组装。请求链只改投影，结果链在 durable 提交前变换；含超时/取消、失败隔离策略、无正文事件与 OTel span。

- 新增稳定 API `tool`、`DecoratedTool`、`ToolError`，从 async 函数签名生成工具。JSON Schema `type` 列表现在真正校验，并继续检查非空 object/array 的内部值；非法 type 列表明确拒绝。复杂类型与可插拔 SchemaValidator 留待后续。

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
