# Learning Path

每一阶段都必须形成可运行的纵向切片，而不是先造一批空接口。

1. **消息、流和最小循环**：理解一次 Agent run 如何开始、提交和 settled。
2. **真实 Provider 适配**：DeepSeek SSE、认证、错误映射、usage、thinking replay 和生命周期。
3. **工具调用**：schema、调度、tool result、失败和权限。
4. **会话与事件日志**：追加式存储、回放、恢复和分叉。
5. **上下文策略**：资源加载、token budget、裁剪与压缩。
6. **Provider 工程化**：重试、退避、限流、多供应商和模型能力。
7. **插件与 Hook**：观察、拦截、生命周期和释放。
8. **安全执行**：审批策略、工作区边界与沙箱。
9. **可观测性与评测**：trace、成本、延迟、回归数据集和评分器。
10. **产品适配与多 Agent**：CLI、Web/SSE、垂直 profile、委派和预算。

当前进度：阶段 1、2、3 已完成；阶段 4 已完成内存与 JSONL SessionStore、乐观并发控制、跨 run/进程恢复、逐消息提交、durable event log、回放、持久化分叉和 replayable compaction event；分支元数据、多进程并发和数据库事务留给后续存储 Provider。阶段 5 已完成 `ContextPlan`、异步 `ContextStrategy`、默认全历史策略、可替换 token 估算、完整 turn 分组、预算投影，以及手动触发的 durable compaction；下一步是自动触发策略、摘要质量评测与资源加载。
