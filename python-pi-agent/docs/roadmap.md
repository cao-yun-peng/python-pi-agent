# 基座化路线图（Foundation Roadmap）

> 状态：草稿（Proposed）
> 评估基线：`eda4a46`（`feat: assemble docker bash from app config`），`pi-agent-py 0.8.0`
> 评估时离线测试：238 passed / 1 skipped（Docker 集成），约 4.6s

## 目标

把 `pi_agent_py` 从“教学优先的完整 Harness”推进为**可被其他项目稳定依赖的基座**：

- 下游项目只通过小而稳定的公开 API 组装 Agent，不需要修改核心模块即可新增 Provider、Tool、策略或拦截逻辑；
- 一次 run 的输入、状态变化、模型/工具/压缩时间线与失败原因，可以被下游 UI、日志和遥测系统按 session/run 订阅；
- 发布、兼容性和质量门禁可由 CI 证明，而不是靠人工约定。

教学目标不变：每个阶段仍遵守 AGENTS.md §5 教学交付契约与 §15 完成定义。

## 现状：已经具备的基座能力

| 维度 | 已有能力 | 证据 |
|---|---|---|
| 状态模型 | durable event log 为事实来源，replay 重建 snapshot；JSONL schema v1–v6 | ADR 0004/0005，`session_transitions.py` |
| 并发与恢复 | 乐观版本、逐消息提交、未决 tool call 恢复、durable steer/follow-up | ADR 0003/0017，`runtime.AgentSession` |
| 观察 | 版本化 `AgentEventEnvelope`、span/duration、durable event link、隔离/脱敏/缓冲 sink | ADR 0013/0021，`events.py` |
| 扩展 | 事务化 `PluginHost`、Session plugin lease、显式 `ProviderCatalog` | ADR 0012/0019/0024 |
| 生命周期 | owned/borrowed 资源、构造回滚、逆序关闭、Session 池 | `lifecycle.py`、`app_runtime.py` |
| 安全 | 审批 → 声明式 Gate → Tool；Docker 进程沙箱 fail-closed | ADR 0014/0020/0026 |
| Provider | 四家适配器 + retry/backoff/route/fallback 组合层 | ADR 0022/0023 |

结论：核心语义已经扎实；主要差距在**接入面**（如何扩展）、**观测消费面**（如何订阅与导出）和**工程保障**（如何证明稳定）。

## 差距清单

优先级：P0 = 影响正确性或阻塞所有后续扩展；P1 = 下游二开的主要摩擦；P2 = 基座能力补全。

### A. 正确性

| ID | 优先级 | 问题 | 证据 | 方向 |
|---|---|---|---|---|
| A1 | P0 | 非 `AgentError` 异常被统一包装为 `ProviderError("model provider failed")`。第三方 `SessionStore`（如 SQLite 抛 `OperationalError`）或 runtime 自身缺陷会被误报为供应商故障，误导重试策略与排障 | `runtime.py:316` `Agent.run`；`_SessionWriter.commit`（`runtime.py:960`）直接透传 Store 异常 | 新增 `InternalError`；Store 调用边界包装为 `SessionStorageError`；只有 Provider 调用边界产生 `ProviderError`。先写回归测试 |
| A2 | P0 | 错误分类缺少 AGENTS.md §10 要求的“内部错误” | `errors.py` | 与 A1 一起补齐，并在 `run_failed` payload 中带稳定 `error_category` |

### B. 二开扩展性

| ID | 优先级 | 问题 | 证据 | 方向 |
|---|---|---|---|---|
| B1 | P0 | 同一组组件在 5 处重复声明并逐个透传，新增一个端口要改 5 个文件 | `Agent.__init__`（`runtime.py:115`）、`AgentSession.__init__`（`runtime.py:1087`）、`AgentProfile`（`profiles.py:29`）、`AgentRuntimeBuilder`（`assembly.py:159`）、`AgentRuntime._new_session`（`app_runtime.py:353`） | 冻结值对象 `AgentComponents`，见 [ADR 0028](adr/0028-unified-agent-components-and-session-spec.md) |
| B2 | P1 | 已实现按 Session 选择组件与 metadata | `open_session(session_id, request=...)`、`tests/test_session_spec.py` | `SessionSpec` + `SessionSpecResolver`，见 ADR 0028 |
| B3 | P1 | 已实现可注册的 Provider 配置解析 | `ProviderRegistration.parse_settings`、`tests/downstream_provider/echo.py` | 开放 Settings/Config 协议与显式 Catalog，见 ADR 0030 |
| B4 | P1 | 配置驱动的工具（Bash）在 Builder 内硬编码 | `_build_configured_bash`（`assembly.py:591`） | 抽象为“带配置 schema 的 capability 注册”，与 B3 同一机制 |
| B5 | P1 | 拦截点只有 `NextTurnHook` 与 `ProviderRequestHook`；缺 run 开始前（记忆/RAG 注入）、tool result 回填前（截断/脱敏）、assistant 完成后（校验） | `ports.py` | 按 AGENTS.md §8 九阶段定义带类型的 middleware 链，统一顺序、超时、错误语义。待独立 ADR |
| B6 | P1 | 工具编写成本高：`FunctionTool` 需手写 `ToolDefinition` 与 JSON Schema；校验器为自研子集 | `tools.py:31`、`tools.py:225` | `@tool` 装饰器从类型标注 / dataclass 生成 schema；`SchemaValidator` 端口，可选 jsonschema/pydantic extra |
| B7 | P1 | 公开签名泄漏内部接线：`Agent.__init__` 接收 `_SteeringInstruction` 与多个 committer 回调 | `runtime.py:115-150` | 公开 `Agent` 只保留用户语义参数；内部接线移入私有构造路径 |
| B8 | P1 | `__init__.py` 平铺导出约 400 行，无稳定/实验区分；`runtime.py` 2206 行承担 loop、session、control、compaction 编排 | `__init__.py`、`runtime.py` | `pi_agent_py.api` 稳定入口 + `__all__`；拆分 `loop / session / control / compaction_orchestration` |

### C. 可观测性

| ID | 优先级 | 问题 | 证据 | 方向 |
|---|---|---|---|---|
| C1 | P0 | 无 run 级订阅：`RuntimeSession.run()` 只返回最终结果，Web/SSE 需从全局唯一 `EventSink` 自行按 session 过滤 | `app_runtime.py:91` | `RunHandle` + `EventHub`，见 [ADR 0029](adr/0029-run-scoped-event-stream-and-telemetry-sink.md) |
| C2 | P1 | 无遥测导出：AGENTS.md §6 列出的 `TelemetrySink` 端口不存在；无 metrics；全仓库未使用标准库 `logging` | `ports.py`；`rg logging src` 无结果 | `TelemetrySink` 端口 + OTel 插件（optional extra）+ `logging` 适配器，见 ADR 0029 |
| C3 | P1 | token 用量只在单次 `model_usage` 事件中，无 session/run 累计与成本估算 | `runtime.py:443` | `UsageLedger` 观察者 + 可注入价格表；累计值进入 `run_completed` |
| C4 | P2 | 无 durable log 查看工具 | — | `python -m pi_agent_py.inspect <jsonl> --session --run` 按 run/turn 展开时间线 |

### D. 工程保障

| ID | 优先级 | 问题 | 证据 | 方向 |
|---|---|---|---|---|
| D1 | P0 | 无 CI；`requires-python>=3.11` 未被验证 | 仓库无 CI 配置 | GitHub Actions：3.11/3.12/3.13 × Linux/Windows，离线测试 |
| D2 | P1 | `pyproject.toml` 无 dev extras、无 ruff/mypy/pyright 配置；系统解释器下测试因未安装包全部 import 失败 | `pyproject.toml` | `[project.optional-dependencies] dev`；ruff + mypy strict（先对 `domain/ports`）；README 写明开发环境 |
| D3 | P1 | 无 CHANGELOG 与 API 稳定性/版本策略 | — | 0.x 期间稳定 API 变更需 CHANGELOG 条目；1.0 起遵循 semver |
| D4 | P1 | 无参考应用证明“基座可复用” | apps 层为空 | `apps/cli`（REPL + 审批交互）与 `apps/web`（FastAPI SSE，消费 C1） |

### E. 基座能力补全（P2，按通用价值排序）

1. MCP 客户端工具适配器（stdio / streamable HTTP）。
2. durable 人工审批状态机（Web 应用的跨请求审批）。
3. 多进程安全的 `SqliteSessionStore`；JSONL 当前无跨进程锁。
4. 通用 OpenAI-compatible Provider 与 Anthropic Provider。
5. 并行工具执行（需先完成资源隔离与取消语义，见 ADR 0002）。
6. 子 Agent / 多 Agent 编排。
7. 结构化输出（schema-constrained final answer）。

## 阶段计划

每个阶段一个短生命周期分支、按单一意图提交；每阶段结束时 `main` 可运行可测试。

### 阶段 0：正确性与门禁（A1、A2、D1、D2）

- A1/A2 已完成（`e49f2c4`）。[失败分类示例](../examples/29_run_failure_classification.py) 可离线复现 Store 故障、run 终态和后续 run 恢复。
- D1/D2 已完成（`041d07e`）。新增 Linux/Windows × Python 3.11–3.13 离线测试矩阵，以及 Ruff 和核心端口的 mypy strict 检查；六组测试均已在本地验证。
- 交付：`InternalError`、Store 边界包装、回归测试；CI 矩阵；dev extras 与 ruff/mypy 基线。
- 验收：第三方 Store 抛原生异常时 `run_failed.error` 为 `SessionStorageError`；CI 在三个 Python 版本上通过。

### 阶段 1：收敛组装面（B1、B7、B8）

- B1/B7 已接入组件组装与 Session 私有接线（`b95622e`）。旧构造保留弃用兼容。
- B8 的稳定导出已完成（`444c7fe`）。`pi_agent_py.api.__all__` 有契约测试，下游应用用例通过稳定入口完成 Provider、插件、Session 和事件组装。
- B8 的 Runtime 拆分已完成（`853e7ab`）。`runtime/` 按 loop、Session、控制和压缩编排划分，兼容导入经回归和 wheel 安装验证。
- 交付：ADR 0028 定稿；`AgentComponents`；拆分 `runtime.py`；`pi_agent_py.api`。
- 验收：新增一个演示端口只需修改 `AgentComponents` 定义与消费处；既有 238 个测试与全部示例不改语义通过；旧构造参数保留一个版本的兼容层。

### 阶段 2：观测消费面（C1、C2、C3）

- C1–C3 已实现，见 [ADR 0029](adr/0029-run-scoped-event-stream-and-telemetry-sink.md) 与[运行观察示例](../examples/30_run_observation.py)。
- 交付：`EventHub`/`RunHandle`；`TelemetrySink` + OTel extra + `logging` 适配器；Runtime 内的 `UsageLedger` 与可注入价格表。
- 验收：FakeProvider 驱动的测试证明 run 级订阅只收到本 run 事件、慢订阅者不阻塞 loop、run settled 后迭代器正常结束；OTel 测试使用 in-memory exporter 校验 span 父子关系。

### 阶段 3：开放扩展点（B2、B3、B4、B5、B6）

- B2 已实现按 Session 解析完整组件与观察 metadata，包含规格冲突拒绝、并发与取消、资源生命周期。见[教程](../tutorials/30-session-spec.md)和[示例](../examples/31_session_spec.py)。
- B3 已实现 ProviderRegistration.parse_settings，内置和外部 Provider 统一通过 Catalog 解析 YAML。见 [ADR 0030](adr/0030-register-provider-settings-parsers.md)、[教程](../tutorials/31-registered-provider-settings.md)和[示例](../examples/32_registered_provider.py)。
- B4、B5、B6 继续按独立能力推进。
- 交付：`SessionSpecResolver`；可注册 Provider/capability 配置；middleware 链 ADR 与实现；`@tool` 装饰器。
- 验收：在 `examples/` 外的独立测试包中新增一个 Provider 与一个配置驱动工具，不修改 `src/pi_agent_py` 任何文件。

### 阶段 4：参考应用与发布（D3、D4、C4）

- 交付：CLI 与 FastAPI SSE 参考应用；`inspect` 工具；CHANGELOG；`0.9.0`。
- 验收：参考应用只 import `pi_agent_py.api`。

### 阶段 5：能力补全（E）

按下游项目的实际需求排序，每项单独 ADR。

## 非目标

- 不追求与 Pi 的包数量或 TypeScript API 对齐；
- 不在阶段 0–2 引入并行工具执行或热重载插件；
- 不把 `EventSink`/遥测通道升级为审计或恢复事实来源——durable log 仍是唯一事实来源。

## 维护

- 每完成一个差距项，在本表中标注完成 commit，并同步 `architecture.md`、`knowledge-map.md` 与 `upstream-mapping.md`；
- 优先级调整需在本文末尾记录日期与原因。
