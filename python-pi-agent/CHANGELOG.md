# 更新记录

## Unreleased

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

  | 工具对象 | 定义模块 |
  |---|---|
  | `FunctionTool`、`ToolHandler` | `pi_agent_py.tools.function` |
  | `DenyByDefaultApprovalPolicy`、`StaticApprovalPolicy` | `pi_agent_py.tools.approval` |
  | `DeclaredSideEffectGate`、`TrustedToolExecutionGate` | `pi_agent_py.tools.gates` |
  | `validate_tool_arguments` | `pi_agent_py.tools.validation` |
  | `SandboxedShellTool` | `pi_agent_py.tools.shell` |

  教程、示例和下游示范包的具体路径在后续任务调整。

- 新增 CapabilityCatalog、类型化 Settings 和 capabilities YAML 节，通过既有 PluginHost 事务注册工具、Hook、Session 插件及资源 cleanup。Bash 迁入内置注册，顶层 bash 配置继续作为兼容入口，和 capabilities.bash 重复配置时拒绝。

- ProviderRegistration 新增 parse_settings，配置加载支持显式 provider_catalog，Settings 和运行配置改用开放 Protocol。Catalog 提供返回新实例的 with_registration，内置四家与外部 Provider 通过统一入口解析，原有 assembly 导入保持对象身份。

- 新增 SessionOpenRequest、SessionSpec 和 SessionSpecResolver，支持每次打开时解析完整组件和观察 metadata。Builder 支持实例注册与基于默认组件的 resolver factory，同 ID 的冲突规格会被拒绝。metadata 作为 envelope 的 JSON 字段序列化并参与日志脱敏。

- `RuntimeSession.submit()` 和 `RuntimeSession.follow_up()` 返回 `RunHandle`，提供 run 事件流、独立结果等待和定向取消。原先使用 `follow_up()` 返回字符串的调用改用句柄的 `run_id`；过渡期可使用 `follow_up_id()`。底层 `AgentSession.follow_up()` 的签名不变。
- 新增 Runtime 事件订阅、结构化日志及可选 OpenTelemetry 遥测适配器，以及 run/Session 用量汇总和可注入的估算价格表。
