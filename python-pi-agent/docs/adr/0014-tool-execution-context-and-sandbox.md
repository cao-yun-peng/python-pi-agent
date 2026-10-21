# ADR 0014: ToolExecutionContext、审批与 Sandbox 执行边界

## 背景

原工具链在参数校验后直接调用 `ApprovalPolicy.review(call, definition)` 和 `tool.execute(arguments)`。审批器与工具拿不到 session、模型请求序号或批次位置；审批决定没有随调用进入执行边界；runtime 也没有 `Sandbox` 端口，所有函数都直接在 Agent 进程中运行。`ToolDefinition.side_effects` 因而只是说明文字，没有强制消费方。

## 决定

1. 增加 `ToolExecutionContext`，包含调用、定义、session id、模型请求序号、调用序号和可选审批决定。
2. 参数校验完成后建立审批快照；审批通过后，从原始已提交调用重新建立执行快照并附加决定，避免审批器修改参数形成 TOCTOU。
3. `ApprovalPolicy.review()` 只接收 context。未配置策略时，受保护工具继续 fail-closed。
4. `Tool.execute()` 接收 arguments 和 context；所有工具只能由 `Sandbox.execute()` 最终调度。
5. 默认 `DeclaredSideEffectSandbox` 只允许 `side_effects="none"`。read/write/external 必须由应用显式允许，可再叠加工具名 allowlist。
6. `InProcessSandbox` 仅用于明确可信工具。两者都不是 OS 隔离；生产环境可通过相同端口替换为子进程、容器或远端执行器。
7. 沙箱拒绝转换成模型可见错误，并发出 `tool_sandbox_denied`；取消继续传播，超时覆盖 sandbox 与 tool 的完整执行范围。

## 备选方案

- 只把 sandbox 对象放入工具 context：工具可以绕过它直接访问文件或网络，无法形成强制执行边界。
- 让 ApprovalPolicy 同时执行工具：审批、资源隔离和业务能力生命周期耦合，难以替换和测试。
- 默认允许所有副作用：兼容性更好，但 `side_effects` 仍不产生最小权限效果。
- 把进程内 allowlist 称为强沙箱：无法阻止恶意 Python 回调，安全承诺不真实。

## 后果

- `FunctionTool` handler 签名变为 `(arguments, context)`，这是有意的公共接口变更。
- 现有 read/write/external 工具必须显式配置 Sandbox，否则会得到模型可见拒绝结果。
- context 是运行时临时状态，不进入 durable transcript；调用意图和工具结果仍由消息日志持久化。
- 默认 provider 能执行纯工具并强制检查声明；它不能限制工具实际使用的系统调用，声明不可信时必须换真正隔离的 provider。

## 验证

- 审批器和工具收到同一调用身份、session、request number 与 call index，执行 context 含批准决定。
- 审批和工具对参数快照的修改不会改变已提交 assistant tool call。
- 默认 sandbox 拒绝 read effect，配置 effect + tool allowlist 后允许。
- timeout 与 cancellation 穿过 sandbox，拒绝事件和结果可观察。
