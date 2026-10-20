# Knowledge Map

| 知识点 | 理论 | 最小实现 | 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|---|
| 消息模型 | 已覆盖 | 已完成文本、tool call 与 assistant thinking | 待扩展多模态和 provider signature | 已完成 | 第 1、2 章 | 能解释为何只提交完整 assistant message，以及 reasoning 为何必须可回放 |
| Provider 端口 | 已覆盖 | 已完成 | DeepSeek 已接入，待多供应商 | 已完成 | 第 1、2 章 | 能独立写一个适配器而不改 runtime |
| 流式事件 | 已覆盖 | 已完成 | 已有 text/thinking/usage/tool call | 已完成 | 第 1、2、3 章 | 能区分 live、capability event 与 durable state |
| DeepSeek 适配 | 已覆盖 | 已完成 SSE、usage、流式工具与 thinking reasoning 回传 | 待 retry、Responses API 和更多停止原因 | 已完成两轮 MockTransport | 第 2、3 章 | 能解释 SSE、错误映射、reasoning replay、密钥与生命周期边界 |
| Agent Loop | 已覆盖 | 已完成 | 已支持顺序工具循环和会话提交，待并行 | 已完成 | 第 1、3、4 章 | 能画出文本、工具和会话恢复三种路径 |
| 取消传播 | 已覆盖 | 已完成 | 已传播到 Provider 与工具 | 已完成 | 第 1、3 章 | 能证明工具取消后 run 一定 settled |
| 工具系统 | 已覆盖 | 已完成 | 待并行、完整 schema、沙箱 | 已完成 | 第 3 章 | 能实现 schema、审批、执行、错误回填和下一轮请求 |
| 会话与回放 | 已覆盖会话、纯状态转换、日志序号、格式版本、恢复和分叉 | 已完成共享 transition、内存/JSONL 日志、assistant thinking replay、跨进程恢复与持久化 fork | 待分支元数据、多进程并发和数据库 Provider | 已覆盖纯函数、v1/v2 解码、恢复、fork 分歧、目标冲突、损坏日志、I/O 错误、并发冲突、失败与取消路径 | 第 4、5、6、7 章 | 能解释 transition 与 Provider 的边界，从 durable log 重建上下文并安全创建独立分支 |
| 上下文策略 | 已覆盖 durable history、请求投影、预算和完整 turn 边界 | 已完成 ContextPlan、异步策略、全历史/预算实现、TokenEstimator 和 capability event | 待精确 tokenizer、资源加载和 provider-aware budget | 已覆盖投影隔离、连续后缀、工具 turn 原子性、预算失败、取消与 AgentSession | 第 8、9 章 | 能替换估算器与选择策略，且不修改 durable history 或拆断工具链 |
| 上下文压缩 | 待开始 | 待开始 | 待开始 | 待开始 | 后续章节 | 能证明压缩前后关键事实不丢失 |
| 插件系统 | 待开始 | 待开始 | 待开始 | 待开始 | 后续章节 | 能实现注册、顺序和可靠释放 |
| 评测 | 待开始 | 待开始 | 待开始 | 待开始 | 后续章节 | 能复现并比较两次 harness 变更 |
