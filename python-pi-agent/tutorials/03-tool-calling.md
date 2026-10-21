# 第三章：实现安全、可回填的工具调用系统

## 本章目标

让模型不只生成文本，还能请求一个受控能力。完整链路是：

```text
ModelProvider
  -> ToolCall(id, name, arguments)
  -> 查找 Tool
  -> JSON Schema 校验
  -> ToolExecutionContext 审批快照
  -> ApprovalPolicy 审批
  -> ToolExecutionContext 执行快照
  -> Sandbox.execute()
  -> Tool.execute(arguments, context)
  -> ToolOutput
  -> commit tool message
  -> prepare_next_turn Hook
  -> 下一次模型请求
  -> 最终 assistant message
```

运行离线示例：

```powershell
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe .\python-pi-agent\examples\03_tool_calling.py
```

## Capability 的五个部分

1. **Definition**：`ToolDefinition` 声明名称、描述、输入 schema、副作用、审批、重试、回放和超时语义。
2. **Provider**：`FunctionTool` 把 async Python 函数适配成工具。
3. **Consumer**：`Agent` 校验、建立 context、审批、经 Sandbox 执行并提交结果。
4. **Lifecycle**：工具在 Agent 构造时显式注入，执行任务跟随 run 的取消和超时作用域。
5. **Evidence**：`tests/test_tool_runtime.py` 覆盖成功、拒绝、无效参数、超时、取消与未知工具。

## 为什么 ToolCall 必须进入 assistant message

工具调用不是临时回调。它是模型已经作出的、可审计的决定。因此 runtime 先提交包含 `tool_calls` 的 assistant message，再提交引用同一 `tool_call_id` 的 tool message。

第二次模型请求看到的历史是：

```text
user       计算 2 + 3
assistant  ToolCall(call-1, add, {a: 2, b: 3})
tool       call-1 -> 5
```

这样会话持久化后才能重建“为什么执行了这个工具”以及“结果属于哪次调用”。

工具结果提交后，runtime 才到达 `prepare_next_turn` 安全点。这个顺序保证 Hook 不会插进 assistant tool call 与对应 tool result 之间。第 11 章会让 `AgentSession` 在该 Hook 中重新检查 token 阈值，并在必要时把 replay 后的压缩历史交给下一次模型请求；第三章的工具校验、审批与执行代码不需要依赖 compaction。

## Schema 校验发生在审批之前

无效参数无需打扰审批者，也绝不能进入工具函数。当前教学实现支持一个明确的 JSON Schema 子集：

- `type`
- `properties`
- `required`
- `additionalProperties`
- `items`
- `enum`

不支持的关键字暂时忽略。生产系统若需要完整 JSON Schema，应换成经过标准测试的 validator，同时保持 `Tool` 和 runtime 契约不变。

## 审批是独立策略，不属于工具 UI

`ApprovalPolicy` 接收完整 `ToolExecutionContext`，只返回批准或拒绝，不负责执行工具。CLI 可以实现交互式询问，Web 服务可以接审批工作流，企业环境可以根据 session、身份与资源策略自动判断。

当工具标记 `requires_approval=True` 且应用没有配置策略时，`DenyByDefaultApprovalPolicy` 必然拒绝。安全默认值不能依赖调用者“记得配置”。

审批通过后仍必须经过 `Sandbox`。默认 sandbox 只允许 `side_effects="none"`；read、write 和 external 工具需要应用显式配置允许范围。详见第 14 章。

## 为什么多数工具错误要回填模型

以下情况会生成 `is_error=True` 的 tool message，然后允许模型继续：

- 工具不存在
- 参数不符合 schema
- 审批拒绝或审批策略异常
- 工具超时
- 工具抛出异常

模型可以据此修正参数、换用其他工具或向用户解释。外部取消不同：它代表调用者要求整个 run 停止，因此取消会传播到正在执行的工具，并直接进入 `run_cancelled` 与 `run_settled`。

## DeepSeek 流式 ToolCall

OpenAI-compatible SSE 会把一个函数的 JSON 参数拆成多个 `arguments` 字符串片段。`DeepSeekProvider` 按 `index` 聚合片段，在 finish 时解析为完整对象，然后才向 runtime 发出一个规范化 `ToolCall`。

供应商格式因此不会泄漏进工具运行时；FakeProvider 和 DeepSeekProvider 使用同一个 `ToolCall` 契约。

## 当前主动省略的复杂度

- 多工具并行和每工具 execution mode
- 工具执行中的增量进度事件
- 完整 JSON Schema 标准支持
- OS/容器级文件、Shell 与网络沙箱（当前只有可替换端口与声明式 effect gate）
- 身份化 RBAC / ABAC 审批
- durable event store 与崩溃恢复
- retry-safe 与 replay-safe 的自动恢复策略

下一章先把工具意图和结果接到会话存储；第 5、6 章再加入 durable event log、replay 与 JSONL 恢复，第 11 章把自动压缩接到本章预留的工具续轮安全点。
