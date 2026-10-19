# ADR 0002: Sequential Tool Loop with Explicit Approval

## Status

Accepted

## Context

模型工具调用会触发文件、网络或业务系统副作用。runtime 需要验证参数、限制权限、传播取消并把结果关联回原调用，同时保持 Provider 和 UI 可替换。

## Decision

- Provider 只向 runtime 输出完整、规范化的 `ToolCall`。
- 每个工具通过 `ToolDefinition` 声明 schema 和安全元数据。
- runtime 按“查找 → 校验 → 审批 → 执行 → 回填”的固定顺序处理。
- 第一版顺序执行多个工具，保证事件顺序、审批和取消语义清晰。
- 工具错误转换成模型可见的 error result；外部取消继续向上传播。
- 受保护工具在没有 ApprovalPolicy 时默认拒绝。

## Alternatives

- Provider 直接执行函数：供应商适配会掌握业务权限并破坏复用边界。
- 一开始并行执行所有调用：吞吐更高，但副作用顺序、批量取消和审批更难解释。
- 工具异常直接终止 run：实现简单，但模型无法修正参数或选择替代工具。

## Consequences

- 每次工具批次可能增加一次模型请求和 token 消耗。
- 顺序执行降低吞吐，但给后续并行策略提供清晰基线。
- schema validator 当前只实现教学所需子集，完整标准可通过同一边界替换。

## Verification

`tests/test_tool_runtime.py` 验证成功、schema 错误、审批拒绝、超时、取消和未知工具；`tests/test_deepseek_provider.py` 验证流式参数片段聚合与工具 schema 请求格式。
