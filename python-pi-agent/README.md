# pi-agent-py

一个教学优先、模块化、可复用的 Python Agent Harness。我们从可以完全看懂的最小循环开始，逐步加入工具、会话、上下文压缩、权限、沙箱、插件、评测与多 Agent 协作。

当前纵向切片包括：

```text
用户输入 -> 消息历史 -> ModelProvider -> 流式事件 -> Assistant 消息 -> settled
逻辑模型请求 -> RoutePolicy -> retry/backoff -> compatible fallback -> attempt trace
DeepSeek/Kimi/GLM Chat Completions -> Provider adapter -> 通用模型事件 -> Agent Loop
TiMi Codex/GPT/Grok Responses -> typed SSE + opaque state -> Agent Loop
agent.yaml + api_key_env -> process env / .env -> 强类型 Provider config
Loaded config + AgentProfile -> AgentRuntimeBuilder -> AgentRuntime -> RuntimeSession lease
模型 ToolCall -> schema 校验 -> 权限审批 -> 工具执行 -> ToolResult -> 模型续写
SessionStore -> 加载历史 -> Agent Loop -> 逐消息提交 -> 下一轮恢复
消息提交 -> DurableSessionEvent -> 有序事件日志 -> replay -> SessionSnapshot
JSONL -> 严格反序列化 -> durable events -> replay -> 跨进程恢复
历史前缀 -> SessionStore.fork -> 独立 durable log -> 分支继续追加
完整历史 -> ContextStrategy -> ContextPlan -> 单次模型请求投影
完整历史 -> turn 分组 -> token budget -> 最近完整上下文投影
旧完整 turns -> 摘要策略 -> context_compacted event -> replay -> 重启后恢复
候选上下文 -> token threshold policy -> run 前/工具续轮前 durable compaction
PluginFactory -> 原子注册 -> Tool / 有序 Hook -> Agent 组装 -> 逆序释放
EventSink -> 异常隔离 -> 日志脱敏 -> 有界后台投递 -> 显式关闭
AgentEventEnvelope -> version/time/source/correlation -> spans -> durable event join
ToolCall -> ToolExecutionContext -> ApprovalPolicy -> ToolExecutionGate -> ToolOutput
DurableSessionEvent -> event/run/turn identity -> provenance -> schema v4 replay
AgentSession -> dispatcher -> steer / follow-up / abort -> settled / idle
agent.yaml -> coding_profile -> BashTool -> SandboxedBashExecutor -> Docker workspace
```

它同时提供脚本化 `FakeProvider`，以及 DeepSeek、Kimi、GLM Chat Completions 和 TiMi Codex/GPT/Grok Responses API 适配器。默认测试仍然确定、离线且不会产生 API 费用。

## 开发与 CI

在仓库根目录创建并激活开发用虚拟环境。Windows PowerShell 使用 `py -3.11 -m venv .venv` 和 `.\.venv\Scripts\Activate.ps1`，Linux 使用 `python3.11 -m venv .venv` 和 `source .venv/bin/activate`。进入 `python-pi-agent` 后运行：

```sh
python -m pip install -e ".[dev,mcp]"
python -m unittest discover -s tests -v
python -m ruff check src tests
python -m mypy
```

GitHub Actions 在 Linux 和 Windows 上用 Python 3.11、3.12、3.13 安装包并执行离线测试。静态检查使用 Python 3.11，Ruff 检查源码与测试，mypy 以 strict 模式检查稳定接口、核心契约、schema 与 MCP 传输/配置模块。常规 CI 中 Docker 集成测试保持跳过，本地可通过 `PI_AGENT_DOCKER_IMAGE` 显式启用。

## 稳定应用入口

MCP tools 支持显式插件或 YAML capability 接入，包含真实 stdio、Streamable HTTP、独立连接 owner、输入/输出校验及结果预算。安装 `.[mcp]` 后运行[stdio 示例 38](examples/38_mcp_stdio_tools.py)、[HTTP 示例 39](examples/39_mcp_http_tools.py)或[YAML 示例 40](examples/40_mcp_yaml_tools.py)；三者使用本地 fixture 和 FakeProvider。学习顺序为[Fake 切片](tutorials/36-mcp-fake-tools.md) → [SDK/传输](tutorials/37-mcp-sdk-transports.md) → [配置与秘密边界](tutorials/38-mcp-configured-capability.md)。默认 Catalog 不自动启用 MCP；应用需显式提供连接策略、秘密来源、审批与 Gate。真实进程/loopback 集成通过 `PI_AGENT_MCP_INTEGRATION=1` 单独开启，不访问付费模型。

下游应用通过 `pi_agent_py.api` 导入 Builder、Runtime、组件规格和扩展端口。稳定导出清单见 [api.py](src/pi_agent_py/api.py)。应用可用 `AgentRuntimeBuilder.with_provider()` 注入自己的 `ModelProvider`，通过 `PluginAPI` 注册工具和 Hook，并通过 `EventSink` 观察运行。

[应用组装示例](examples/24_application_runtime.py) 演示同一 Session 的复用和独立 Agent。离线测试替身由 `pi_agent_py.testing` 提供。

`RuntimeSession.submit()` 和 `follow_up()` 返回可订阅、可定向取消的 `RunHandle`；`session.usage` 汇总本 Runtime 中该 Session 的用量。参见[观测示例](examples/30_run_observation.py)和[教程](tutorials/29-run-observation.md)。OpenTelemetry 适配器可通过 `pip install ".[otel]"` 安装。

按角色或租户选择 Session 组件时，注册 `SessionSpecResolver` 并向 `open_session(..., request=SessionOpenRequest(...))` 传递选择信息。组件规格包含工具、Provider、prompt 和观察 metadata，运行入口及事件可通过[双角色示例](examples/31_session_spec.py)和[第 30 章](tutorials/30-session-spec.md)学习。

## 快速开始

通过 YAML 启用自定义工具或 Hook 时，可注册 CapabilityRegistration，并把 capability_catalog 传给加载器与 Builder。[配置驱动能力示例](examples/33_configured_capability.py)展示初始化、工具调用、关闭和失败回滚，[第 32 章](tutorials/32-configured-capabilities.md)解释完整契约。

通过 YAML 接入自定义模型供应商时，为 ProviderRegistration 提供 `parse_settings` 和 `factory`，并把同一 Catalog 传给配置加载器与 Builder。[外部 Provider 示例](examples/32_registered_provider.py)和[第 31 章](tutorials/31-registered-provider-settings.md)可离线运行。

在仓库根目录执行。首次使用可以先安装为 editable package：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\python-pi-agent
.\.venv\Scripts\python.exe -m unittest discover -s .\python-pi-agent\tests -v
.\.venv\Scripts\python.exe .\python-pi-agent\examples\01_minimal_loop.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\03_tool_calling.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\04_session_resume.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\05_session_replay.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\06_jsonl_persistence.py --help
.\.venv\Scripts\python.exe .\python-pi-agent\examples\07_session_fork.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\08_context_strategy.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\09_token_budget_context.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\10_durable_compaction.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\11_automatic_compaction.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\12_plugin_system.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\13_safe_event_logging.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\14_tool_execution_security.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\15_durable_event_provenance.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\16_session_control.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\17_durable_controls.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\18_provider_request_context.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\19_session_aware_plugin.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\20_docker_workspace_sandbox.py --help
.\.venv\Scripts\python.exe .\python-pi-agent\examples\21_minimal_http_sse.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\22_provider_configuration.py --help
.\.venv\Scripts\python.exe .\python-pi-agent\examples\23_provider_resilience.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\24_application_runtime.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\26_bash_tool_contract.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\27_docker_bash_executor.py --help
.\.venv\Scripts\python.exe .\python-pi-agent\examples\28_configured_bash_runtime.py --help
.\.venv\Scripts\python.exe .\python-pi-agent\examples\29_run_failure_classification.py
```

已经安装 `httpx` 依赖时，也可以直接用源码目录运行：

```powershell
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe -m unittest discover -s .\python-pi-agent\tests -v
.\.venv\Scripts\python.exe .\python-pi-agent\examples\01_minimal_loop.py
```

连接 DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe .\python-pi-agent\examples\02_deepseek_chat.py
```

默认使用当前官方模型名 `deepseek-flash`，可通过 `DEEPSEEK_MODEL` 修改。详见[第二章：DeepSeekProvider](tutorials/02-deepseek-provider.md)。

使用仓库顶层 YAML + `.env` 统一配置 DeepSeek/Kimi/GLM/TiMi：

```powershell
Copy-Item .\agent.yaml.example .\agent.yaml
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
python .\python-pi-agent\examples\22_provider_configuration.py set-key
python .\python-pi-agent\examples\22_provider_configuration.py check
```

YAML 只保存 `api_key_env`，真实密钥写入被 Git 忽略的 `.env`；`check` 只打印脱敏值和来源。TiMi 使用 `name: timicc`、`TIMI_CC_API_KEY`，并要求显式填写 key 分组实际可用的 Codex/GPT/Grok 模型名。详见[顶层 Provider 配置教程](tutorials/21-provider-configuration.md)、[GLM 标准 API 适配教程](tutorials/22-glm-standard-api.md)与 [TiMi Responses API 适配教程](tutorials/23-timicc-responses-api.md)。

想先看清“线路上到底传了什么”，可以运行不依赖 Agent 框架的最小客户端：

```powershell
# 离线 fixture，不访问网络
.\.venv\Scripts\python.exe .\python-pi-agent\examples\21_minimal_http_sse.py
# 真实调用
$env:DEEPSEEK_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe .\python-pi-agent\examples\21_minimal_http_sse.py --live
```

预期示例输出：

```text
你好，我是最小 Agent。

--- trace ---
live    run_started
durable message_committed
capability context_prepared
live    model_request_started
live    text_delta
live    text_delta
live    model_request_finished
durable message_committed
live    run_completed
live    run_settled
```

## 从哪里开始读

1. [教程：最小 Agent Loop](tutorials/01-minimal-agent-loop.md)
2. `src/pi_agent_py/domain.py`：什么是消息和事件
3. `src/pi_agent_py/ports.py`：核心依赖哪些抽象，以及观察端口与拦截 Hook 的区别
4. `src/pi_agent_py/runtime/loop.py`：循环如何消费流、提交消息，并在下一次模型请求前调用 `NextTurnHook`
5. `src/pi_agent_py/session_transitions.py`：会话事件怎样纯计算出新状态
6. `src/pi_agent_py/testing.py`：如何不用真实模型验证循环
7. `tests/test_minimal_loop.py`：公开行为契约
8. `src/pi_agent_py/providers/deepseek.py`：真实 SSE Provider 适配器
9. `src/pi_agent_py/model_runtime.py`：retry、backoff、能力路由、fallback 与 attempt trace
10. `src/pi_agent_py/profiles.py`：产品默认值为何只保存 factory、不持有活 I/O
11. `src/pi_agent_py/assembly.py`：显式 Catalog、事务化 Builder 与失败回滚
12. `src/pi_agent_py/app_runtime.py` 与 `src/pi_agent_py/runtime/`：应用 facade、Session lease、控制队列和压缩编排
13. `src/pi_agent_py/lifecycle.py`：owned/borrowed 资源与逆序释放
14. `src/pi_agent_py/tools.py`：工具定义、审批策略、执行 Gate 与兼容期 Shell consumer
15. `src/pi_agent_py/shell.py` 与 `sandbox.py`：BashTool、ShellExecutor 组装、workspace 路径解析和 exact-argv 进程隔离
16. [教程：工具调用系统](tutorials/03-tool-calling.md)
17. [教程：会话存储与 AgentSession](tutorials/04-session-runtime.md)
18. [教程：Durable Event Log 与 Replay](tutorials/05-durable-event-log.md)
19. [教程：JSONL 持久化](tutorials/06-jsonl-persistence.md)
20. [教程：持久化会话分叉](tutorials/07-session-fork.md)
21. [教程：ContextStrategy 接缝](tutorials/08-context-strategy.md)
22. [教程：Token Budget 与完整消息组](tutorials/09-token-budget-context.md)
23. [教程：Durable Compaction](tutorials/10-durable-compaction.md)
24. [教程：Token Threshold 自动压缩与 NextTurnHook](tutorials/11-automatic-compaction.md)
25. [教程：显式、可释放的插件系统](tutorials/12-plugin-system.md)
26. [教程：隔离 EventSink 与安全日志](tutorials/13-safe-event-observation.md)
27. [教程：ToolExecutionContext、审批与执行 Gate](tutorials/14-tool-execution-security.md)
28. [教程：Durable Event 的 Run、Turn 与 Provenance](tutorials/15-durable-event-provenance.md)
29. [教程：AgentSession 控制状态机](tutorials/16-agent-session-control.md)
30. [教程：Steer 与 Follow-up 的持久化恢复](tutorials/17-durable-session-controls.md)
31. [教程：ProviderRequestContext](tutorials/18-provider-request-context.md)
32. [教程：Session-aware 插件与生命周期租约](tutorials/19-session-aware-plugins.md)
33. [教程：Docker Workspace Process Sandbox](tutorials/20-workspace-process-sandbox.md)
34. [Compaction 质量评测方案](docs/compaction-evaluation.md)
35. [教程：GLM 标准 API 适配](tutorials/22-glm-standard-api.md)
36. [教程：TiMi Codex/GPT/Grok Responses API 适配](tutorials/23-timicc-responses-api.md)
37. [教程：Provider Retry、Backoff、Router 与 Fallback](tutorials/24-provider-resilience.md)
38. [教程：AgentRuntime、Profile 与统一生命周期](tutorials/25-application-runtime-assembly.md)
39. [教程：BashTool 与 ShellExecutor 契约](tutorials/26-bash-tool-contract.md)
40. [教程：Docker-backed BashExecutor](tutorials/27-docker-bash-executor.md)
41. [教程：从 agent.yaml 组装 Docker Bash Runtime](tutorials/28-configured-bash-runtime.md)

项目路线见 [docs/learning-path.md](docs/learning-path.md)，架构边界见 [docs/architecture.md](docs/architecture.md)。

类型化工具声明见[第 33 章](tutorials/33-tool-decorator.md)与[示例 34](examples/34_tool_decorator.py)；组合检索投影和结果截断见[第 34 章](tutorials/34-middleware.md)与[示例 35](examples/35_middleware.py)。两者均离线运行。

远端工具接入前的通用契约见[第 35 章](tutorials/35-generic-tool-adapter-seams.md)和[示例 36](examples/36_generic_tool_seams.py)：可替换 schema 校验、插件观察借用、工具 run/turn 身份与未知超时结果。完整校验器需可选安装 [jsonschema]，基础 API 无该依赖仍可导入。

workspace 文件端口和 `read` 工具见[第 39 章](tutorials/39-workspace-read.md)与[示例 42](examples/42_workspace_read.py)。默认以内存文件和 FakeProvider 离线展示分页、版本、错误与事件；显式选择本地 Docker Python 镜像可读取真实 workspace。`read_only_profile()` 只放行已注册的 read，`coding_profile()` 支持显式注册的 bash/read。
