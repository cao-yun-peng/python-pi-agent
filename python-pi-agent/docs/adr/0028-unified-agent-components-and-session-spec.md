# ADR 0028: 统一 Agent 组件规格与按 Session 解析配置

> 状态：已接受（Accepted）
> 关联：[路线图](../roadmap.md) B1、B2、B7；ADR 0016、0019、0024
> 已实现：统一组件组装与兼容层，以及阶段 3 B2 的按 Session 规格解析和观察 metadata。

## 背景

当前同一组运行时组件在五处重复声明并逐个透传：

| 位置 | 角色 |
|---|---|
| `runtime.py` `Agent.__init__` | loop 消费者 |
| `runtime.py` `AgentSession.__init__` | Session 编排，再转交 `Agent` |
| `profiles.py` `AgentProfile` | 产品默认值（factory 形式） |
| `assembly.py` `AgentRuntimeBuilder` | 应用组装与覆盖 |
| `app_runtime.py` `AgentRuntime._new_session` | 每个 Session 再次透传 |

后果：

1. 新增一个可替换端口（例如未来的 middleware、`SchemaValidator`）需要修改五个文件，且容易漏掉某一层，造成“Builder 能配置但 Session 不生效”一类缺陷；
2. `AgentRuntime` 中所有 Session 共享同一 system prompt、工具集和 Provider，无法按用户、租户或角色区分，也无法携带不进入模型上下文的 Session 元数据；
3. `Agent.__init__` 公开签名中出现私有类型 `_SteeringInstruction` 与 committer 回调，把 `AgentSession` 的内部接线暴露为公共 API。

## 决定

### 1. 引入冻结值对象 `AgentComponents`

```python
@dataclass(frozen=True, slots=True)
class AgentComponents:
    provider: ModelProvider
    tools: tuple[Tool, ...] = ()
    approval_policy: ApprovalPolicy = DenyByDefaultApprovalPolicy()
    execution_gate: ToolExecutionGate = DeclaredSideEffectGate()
    context_strategy: ContextStrategy = FullHistoryContextStrategy()
    compaction: CompactionComponents | None = None   # strategy 必填，policy 用于自动触发
    next_turn_hook: NextTurnHook | None = None
    system_prompt: str | None = None
    max_model_requests: int = 8

    def replace(self, **changes) -> AgentComponents: ...
```

- 只包含**模型可见行为**相关的端口与参数；`SessionStore`、`EventSink`、`PluginHost`、clock 等基础设施归属另一个对象（暂名 `RuntimeServices`），因为它们的生命周期属于 Runtime 而非单次组装；
- 构造时校验工具重名和正整数上限。手动压缩只需摘要 strategy，自动触发的 policy 需要同时提供 strategy；
- `AgentProfile` 保持 factory 形式（不持有活 I/O，ADR 0024），新增 `profile.build_components(provider) -> AgentComponents`；
- `Agent`、`AgentSession`、`AgentRuntime` 只接收 `AgentComponents`，新增端口时只修改定义与实际消费处。

### 2. 公开 `Agent` 只保留用户语义

- 公开构造：`Agent(components, *, event_sink=None, session_id=None, initial_messages=())`；
- `message_committer`、`steering_committer`、`steering_receiver`、`provider_request_hook` 移入私有的 `_AgentWiring`，仅由 `AgentSession` 通过模块内构造路径传入；
- 旧关键字参数保留一个 minor 版本，发出 `DeprecationWarning` 后移除。

### 3. 按 Session 解析组件：`SessionSpec` 与 `SessionSpecResolver`

```python
@dataclass(frozen=True, slots=True)
class SessionSpec:
    components: AgentComponents
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)

class SessionSpecResolver(Protocol):
    async def resolve(self, session_id: str, request: SessionOpenRequest) -> SessionSpec: ...
```

- 默认 `StaticSessionSpecResolver(base_components)`，行为与当前完全一致；
- `AgentRuntime.open_session(session_id, *, request=SessionOpenRequest(...))`；`request` 携带调用方声明的 user/tenant/profile 选择等非秘密字段；
- 解析结果在 Session controller 生命周期内固定；同一 `session_id` 被不同 spec 重复打开时拒绝（`RuntimeStateError`），避免池内 controller 静默串用配置；
- `metadata` 进入事件 envelope 的观察上下文（受脱敏策略约束），不写入 durable message，也不传给 Provider。

### 4. B2 的实现契约

- `SessionOpenRequest` 位于 domain，字段为可选的 `user_id`、`tenant_id`、`profile` 和 JSON `metadata`。resolver 验证调用方权限并选择输出 metadata。默认静态实现使用组装组件和空 metadata。
- 每次打开均在池锁外解析，在锁内比较和复用。能力端口、工具按实例身份比较，compaction 比较 strategy/policy 实例，prompt 和上限按值比较，metadata 按 JSON 内容比较。冲突检查先于 TTL 驱逐，已留在池内的 controller 仍保持原规格。
- Builder 可通过 `with_session_spec_resolver()` 注册 owned/borrowed 实例，或通过 `with_session_spec_resolver_factory()` 从完整默认组件构造 owned resolver。factory 接收到已组合的 Provider、Profile 和插件工具/Hook。输出规格完整替换默认组件选择。
- SessionSpec 内的能力资源由应用或 resolver 管理。Runtime 关闭先取消并等待尚未完成的解析，再关闭 controller 和 owned 资源。原生解析异常包装为 `ConfigurationError` 并保留 cause，领域异常与取消继续传播。
- 请求、规格和 envelope 的 metadata 在边界校验为有限 JSON，并保存独立快照，嵌套读取返回副本。应用决定字段 schema 和大小。观察上下文通过 Runtime 私有 ContextVar 传播到调度器、模型组合层和插件任务，直接 controller 事件还使用绑定的 sink。
- envelope metadata 是观察 JSON record 的可选上下文。日志脱敏覆盖其敏感键，遥测投影清空这些自由字段。Provider 请求与 durable history 的输入由正式模型接口提供。
- 当 resolver 替换默认 Provider 时，用量账本继续统计 token，成本标记为缺价。默认模型的价格仅适用于沿用默认 Provider 的组件。

## 备选方案

1. **保持关键字参数，靠测试覆盖遗漏**：拒绝。问题在结构上，测试只能事后发现漏传。
2. **用 `**kwargs` 或 dict 透传**：拒绝。失去类型检查，违反 AGENTS.md §10“核心契约避免无边界 Any”。
3. **依赖注入容器**：拒绝。引入隐式解析与全局注册，违反 §6“组装必须显式”。
4. **把 `SessionStore`/`EventSink` 也放进 `AgentComponents`**：暂不采用。它们由 Runtime 拥有并跨 Session 共享，混入后 `replace()` 会制造“同一 Runtime 多个 Store”的歧义。
5. **允许运行中修改 Session 组件**：拒绝。会让 replay 无法确定某轮使用的工具集；切换配置应通过 fork 或新 Session 完成。

## 后果

- 新增端口的修改面从五个文件降到“定义 + 消费处”；
- 多租户/多角色应用可以在不修改核心的情况下按 Session 选择工具与 prompt；
- `Agent` 公开签名变小，`AgentSession` 内部接线可以自由演进；
- 需要一个版本的兼容层与迁移说明，现有示例与教程中的构造代码需要更新；
- `SessionSpec` 不 durable：重启后由 resolver 重新解析，resolver 必须对同一输入给出确定结果，否则恢复后的行为可能变化——这一约束需要在教程中明确。

## 验证方式

- 组件不变量测试：重名工具、非法上限在构造 `AgentComponents` 时失败，缺少摘要 strategy 在构造 `CompactionComponents` 时失败；
- 透传完整性测试：对每个 `AgentComponents` 字段，使用可观察的假实现，从 `AgentRuntimeBuilder` 一路断言到 `Agent` 实际调用（参数化覆盖全部字段，新增字段未覆盖则测试失败）；
- `SessionSpecResolver` 测试：两个 Session 使用不同工具集互不可见；同 ID 不同 spec 重复打开被拒绝；metadata 不出现在 Provider 请求与 durable log 中；
- 兼容性测试：旧关键字参数路径产生 `DeprecationWarning` 且行为不变；
- 既有离线测试全部通过。

## 验证证据

`tests/test_session_spec.py` 覆盖能力选择、metadata 的深层快照和脱敏、Provider/durable/telemetry 可见性、全部组件字段的冲突检测、并发打开、池外慢解析、取消、关闭、借用/拥有生命周期及构造回滚。[第 30 章](../../tutorials/30-session-spec.md) 和离线示例导入正式包复现这些机制。

`RuntimeServices` 继续由应用层内部结构承担。应用负责 metadata schema 和身份授权。持久化规格版本与跨 Runtime 的租户 namespace 留到后续设计。
