# Knowledge Map

| 知识点 | 理论 | 最小实现 | 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|---|
| 消息模型 | 已覆盖 | 已完成文本、tool call 与 assistant thinking | 待扩展多模态和 provider signature | 已完成 | 第 1、2 章 | 能解释为何只提交完整 assistant message，以及 reasoning 为何必须可回放 |
| Provider 端口 | 已覆盖 | 已完成 | DeepSeek 已接入，待多供应商 | 已完成 | 第 1、2 章 | 能独立写一个适配器而不改 runtime |
| 流式事件 | 已覆盖 | 已完成 | 已有 text/thinking/usage/tool call | 已完成 | 第 1、2、3 章 | 能区分 live、capability event 与 durable state |
| DeepSeek 适配 | 已覆盖 | 已完成 SSE、usage、流式工具与 thinking reasoning 回传 | 待 retry、Responses API 和更多停止原因 | 已完成两轮 MockTransport | 第 2、3 章 | 能解释 SSE、错误映射、reasoning replay、密钥与生命周期边界 |
| Agent Loop | 已覆盖 | 已完成 | 已支持顺序工具循环、会话提交和 next-turn 拦截接缝，待并行 | 已完成 | 第 1、3、4、11 章 | 能画出文本、工具、会话恢复及工具续轮前 Hook 路径 |
| 取消传播 | 已覆盖 | 已完成 | 已传播到 Provider 与工具 | 已完成 | 第 1、3 章 | 能证明工具取消后 run 一定 settled |
| 工具系统 | 已覆盖 | 已完成 | 待并行、完整 schema、沙箱 | 已完成 | 第 3 章 | 能实现 schema、审批、执行、错误回填和下一轮请求 |
| 会话与回放 | 已覆盖会话、纯状态转换、日志序号、格式版本、恢复、分叉和替换事件 | 已完成共享 transition、内存/JSONL 日志、assistant thinking replay、跨进程恢复、持久化 fork 与 compaction replay | 待分支元数据、多进程并发和数据库 Provider | 已覆盖纯函数、v1/v2/v3 解码、恢复、fork、compaction、损坏日志、I/O、并发冲突、失败与取消 | 第 4、5、6、7、10 章 | 能解释 transition 与 Provider 的边界，从 durable log 重建压缩或分叉后的上下文 |
| 上下文策略 | 已覆盖 durable history、请求投影、预算和完整 turn 边界 | 已完成 ContextPlan、异步策略、全历史/预算实现、TokenEstimator 和 capability event | 待精确 tokenizer、资源加载和 provider-aware budget | 已覆盖投影隔离、连续后缀、工具 turn 原子性、预算失败、取消与 AgentSession | 第 8、9 章 | 能替换估算器与选择策略，且不修改 durable history 或拆断工具链 |
| 上下文压缩 | 已覆盖临时投影、完整 turn 边界、触发/摘要端口、追加式替换和乐观并发 | 已完成手动/阈值自动 durable compaction、工具续轮前触发、模型/假摘要策略、JSONL v3 与 replay | 待增量摘要、精确 tokenizer、overflow 恢复和质量 eval 实现 | 已覆盖阈值决策、跳过、选择、模型协议、同轮工具触发、Store 重启、失败与取消原子性 | 第 10、11 章 | 能分离 when/how/persist/replay，并解释为何 Hook 位于 tool result 与下一次请求之间 |
| 插件系统 | 已覆盖 factory/API/host、原子注册、顺序、超时、失败关闭与释放 | 已完成 Tool + NextTurnHook + cleanup 的显式纵向切片 | 待 session-aware durable hook、观察订阅、资源发现、runtime lease 与热重载 | 已覆盖端到端组装、顺序、冲突回滚、超时、取消、注销与逆序释放 | 第 12 章 | 能独立实现插件注册事务，解释拦截/观察错误语义，并证明卸载不会遗留 host 注册或资源 |
| 评测 | 已完成 compaction 的结构、probe QA、模型裁判和 trace 分层方案 | 待实现离线 eval case 与 deterministic grader | 待数据集、真实模型 judge、持续评测与报告版本化 | 当前只有 runtime 契约测试 | `docs/compaction-evaluation.md` | 能用版本化数据比较摘要 prompt/模型/策略，并让 judge 与人工标签校准 |
