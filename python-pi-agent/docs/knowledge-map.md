# Knowledge Map

| 知识点 | 理论 | 最小实现 | 完整实现 | 测试 | 教程 | 掌握标准 |
|---|---|---|---|---|---|---|
| 消息模型 | 已覆盖 | 已完成文本、tool call 与 assistant thinking | 待扩展多模态和 provider signature | 已完成 | 第 1、2 章 | 能解释为何只提交完整 assistant message，以及 reasoning 为何必须可回放 |
| Provider 端口 | 已覆盖 | 已完成 | DeepSeek 已接入，待多供应商 | 已完成 | 第 1、2 章 | 能独立写一个适配器而不改 runtime |
| 流式事件 | 已覆盖 | 已完成 | 已有 text/thinking/usage/tool call | 已完成 | 第 1、2、3 章 | 能区分 live、capability event 与 durable state |
| DeepSeek 适配 | 已覆盖 | 已完成 SSE、usage、流式工具与 thinking reasoning 回传 | 待 retry、Responses API 和更多停止原因 | 已完成两轮 MockTransport | 第 2、3 章 | 能解释 SSE、错误映射、reasoning replay、密钥与生命周期边界 |
| Agent Loop | 已覆盖 | 已完成 | 已支持顺序工具循环、会话提交和 next-turn 拦截接缝，待并行 | 已完成 | 第 1、3、4、11 章 | 能画出文本、工具、会话恢复及工具续轮前 Hook 路径 |
| 取消传播 | 已覆盖 | 已完成 | 已传播到 Provider 与工具 | 已完成 | 第 1、3 章 | 能证明工具取消后 run 一定 settled |
| 工具系统 | 已覆盖 schema、上下文快照、审批、声明式副作用边界、超时与取消 | 已完成 ToolExecutionContext → ApprovalPolicy → Sandbox → Tool 管线 | 待 OS/容器隔离、身份化 RBAC/ABAC、并行、完整 schema 与 durable approval | 已覆盖成功、拒绝、策略失败关闭、默认 sandbox 拒绝、显式 allowlist、参数快照隔离、超时和取消 | 第 3、14 章 | 能实现 validate/approve/sandbox/execute 管线，并区分 effect gate 与真正系统隔离 |
| 会话与回放 | 已覆盖会话、纯状态转换、日志序号、event/run/turn/provenance、格式版本、恢复、分叉、替换事件和未决工具调用 | 已完成共享 transition、schema v4 内存/JSONL 日志、assistant thinking replay、跨进程恢复、尾部未决工具结果修复、带来源链接的 fork 与 compaction replay | 待 durable tool-started 事实、多进程并发和数据库 Provider | 已覆盖纯函数、v1-v4 解码、metadata round-trip、恢复、未决工具批次、fork、compaction、损坏日志、I/O、并发冲突、失败与取消 | 第 4、5、6、7、10、15 章 | 能解释 transition 与 Provider 的边界，用 run/turn/provenance 追踪 durable facts，并从日志重建压缩或分叉后的上下文 |
| Session 控制面 | 已覆盖 prompt/steer/follow-up/abort、run settled 与 session idle | 已完成 FIFO dispatcher、活动任务取消、安全边界 steer 和 idle barrier | 待 durable queue、retry queue、图片输入与跨进程控制 | 已覆盖 abort、FIFO follow-up、工具中 steer、idle 拒绝和队列清理 | 第 16 章 | 能区分 steer/follow-up/abort，并证明成功接受的 steer 不会在最终 turn 竞态中丢失 |
| 上下文策略 | 已覆盖 durable history、请求投影、预算和完整 turn 边界 | 已完成 ContextPlan、异步策略、全历史/预算实现、TokenEstimator 和 capability event | 待精确 tokenizer、资源加载和 provider-aware budget | 已覆盖投影隔离、连续后缀、工具 turn 原子性、预算失败、取消与 AgentSession | 第 8、9 章 | 能替换估算器与选择策略，且不修改 durable history 或拆断工具链 |
| 上下文压缩 | 已覆盖临时投影、完整 turn 边界、触发/摘要端口、追加式替换和乐观并发 | 已完成手动/阈值自动 durable compaction、工具续轮前触发、模型/假摘要策略、JSONL v4 与 replay | 待增量摘要、精确 tokenizer、overflow 恢复和质量 eval 实现 | 已覆盖阈值决策、跳过、选择、模型协议、同轮工具触发、Store 重启、失败与取消原子性 | 第 10、11 章 | 能分离 when/how/persist/replay，并解释为何 Hook 位于 tool result 与下一次请求之间 |
| 插件系统 | 已覆盖 factory/API/host、原子注册、顺序、超时、失败关闭与释放 | 已完成 Tool + NextTurnHook + cleanup 的显式纵向切片 | 待 session-aware durable hook、观察订阅、资源发现、runtime lease 与热重载 | 已覆盖端到端组装、顺序、冲突回滚、超时、取消、注销与逆序释放 | 第 12 章 | 能独立实现插件注册事务，解释拦截/观察错误语义，并证明卸载不会遗留 host 注册或资源 |
| 安全事件观察 | 已覆盖观察/拦截分离、失败隔离、数据最小化、背压与生命周期 | 已完成 Isolated/Redacting/Buffered EventSink 组合 | 待 telemetry schema、指标聚合、审计型 durable sink 与跨线程压力测试 | 已覆盖 Agent/Session/Plugin 隔离、原错保留、脱敏不变性、顺序投递、下游失败与关闭后丢弃 | 第 13 章 | 能组装先脱敏后入队的日志链路，并解释 best-effort 事件为何不能承担恢复事实 |
| 评测 | 已完成 compaction 的结构、probe QA、模型裁判和 trace 分层方案 | 待实现离线 eval case 与 deterministic grader | 待数据集、真实模型 judge、持续评测与报告版本化 | 当前只有 runtime 契约测试 | `docs/compaction-evaluation.md` | 能用版本化数据比较摘要 prompt/模型/策略，并让 judge 与人工标签校准 |
