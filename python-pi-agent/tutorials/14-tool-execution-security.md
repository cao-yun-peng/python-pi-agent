# 第十四章：ToolExecutionContext、审批与 Sandbox

## 1. 原来的安全元数据没有执行力

旧链路虽然有 `requires_approval` 和 `side_effects`，但执行仍然是：

```python
decision = await approval_policy.review(call, definition)
output = await tool.execute(call.arguments)
```

这里有三个结构性问题：

1. 审批器看不到 session、当前模型请求和工具在批次中的位置。
2. 工具不知道自己对应哪次调用，也拿不到最终审批决定。
3. `side_effects` 没有任何强制消费方；审批通过后直接执行 Python 回调。

因此它更像“带注释的函数调用”，还不是完整的安全执行管线。

## 2. 新的固定阶段

现在 runtime 使用以下顺序：

```text
resolve Tool
  -> validate arguments
  -> build approval ToolExecutionContext snapshot
  -> ApprovalPolicy.review(context)（仅 requires_approval）
  -> rebuild execution snapshot + approval decision
  -> Sandbox.execute(tool, arguments, context)
  -> Tool.execute(arguments, context)
  -> validate ToolOutput
  -> commit tool result
```

审批拒绝时不会进入 Sandbox；沙箱拒绝时不会进入 Tool。超时包围 Sandbox 与 Tool 的完整执行，外部取消沿同一 asyncio task 传播。

## 3. ToolExecutionContext 里有什么

```python
@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    call: ToolCall
    definition: ToolDefinition
    request_number: int
    call_index: int
    session_id: str | None = None
    approval: ApprovalDecision | None = None
```

`request_number` 表示当前 run 的第几次模型请求；`call_index` 是该 assistant tool batch 中从 1 开始的位置。`AgentSession` 会自动把 session id 传入 Agent，普通 Agent 也可显式配置。

context 外壳是 frozen，但 JSON 容器本身可变。runtime 因此不会把 transcript 中的原对象直接交给外部代码：审批和执行分别使用深拷贝快照。审批器即使错误地修改参数，也不会改变执行参数；工具修改 arguments 也不会污染已经提交的 assistant message。

## 4. ApprovalPolicy 只作决定

策略接口现在只接收 context：

```python
class ApprovalPolicy(Protocol):
    async def review(
        self,
        context: ToolExecutionContext,
    ) -> ApprovalDecision: ...
```

策略可以根据 `context.session_id`、tool name、副作用级别和参数决定是否批准，但不负责执行工具。策略异常、返回错误类型或应用未配置受保护工具策略时都会 fail-closed。取消不是拒绝：`CancelledError` 会继续终止整个 run。

审批目前仍是 live capability decision，不是 durable fact。若未来需要人工审批跨进程等待，必须新增可持久化的 approval state machine，不能只保存内存中的 `ApprovalDecision`。

## 5. Sandbox 是唯一执行入口

端口定义：

```python
class Sandbox(Protocol):
    async def execute(
        self,
        tool: Tool,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput: ...
```

runtime 不再直接调用 `tool.execute()`。默认 `DeclaredSideEffectSandbox`：

- 只允许 `side_effects="none"`；
- 可显式允许 read/write/external；
- 可进一步限制工具名；
- 对 `requires_approval=True` 的工具再次检查 context 中存在批准决定。

配置示例：

```python
sandbox = DeclaredSideEffectSandbox(
    allowed_side_effects=("none", "read"),
    allowed_tools=("read_note",),
)
```

这是一条**声明式策略边界**，仍在当前 Python 进程执行。它无法阻止一个谎报 `side_effects="none"` 的恶意 handler 访问网络或文件。对不可信代码必须实现真正的 Sandbox provider，例如受限子进程、容器或远端 worker，并在那里控制文件系统、网络、环境变量和资源限额。

`InProcessSandbox` 显式允许所有声明的副作用，仅适用于调用者已经信任工具实现的场景；它仍会检查受保护调用已获批准。

## 6. 工具怎样使用 context

```python
async def save_note(
    arguments: JSONObject,
    context: ToolExecutionContext,
) -> ToolOutput:
    assert context.approval is not None
    assert context.approval.approved
    return ToolOutput(content=f"saved: {arguments['text']}")
```

完整组装见 `examples/14_tool_execution_security.py`。注意审批与 Sandbox 是两个独立闸门：用户批准不等于应用自动扩大执行权限，sandbox 仍必须允许对应 effect 和 tool。

## 7. 与 Pi 的映射

固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 中：

- core `AgentTool.execute()` 接收 `toolCallId`、参数、AbortSignal 和 update callback；
- coding-agent 的 `ExtensionToolContext` 再提供 cwd、session、signal 和嵌套工具调用；
- `beforeToolCall` context 包含 assistant message、raw call、validated args 和 AgentContext。

本项目暂不复制 UI、嵌套调用和 streaming update。它先把最小身份快照、审批决定和可替换执行端口建立起来，并额外使用声明式 effect allowlist 作为默认拒绝边界。

## 8. 验证和掌握标准

运行：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\14_tool_execution_security.py
.\.venv\Scripts\python.exe -m unittest python-pi-agent/tests/test_tool_runtime.py -v
```

掌握标准：

- 能解释为什么顺序必须是 validate → approve → sandbox → execute。
- 能证明审批器不能通过修改参数改变实际执行内容。
- 能区分用户批准、声明式 effect allowlist 与真正的 OS 隔离。
- 能说明 sandbox 拒绝为何回填 tool error，而外部取消为何直接传播。
- 能实现一个新的 Sandbox provider，而不修改 Agent Loop 或 Tool 实现。
