# ADR 0049 Run 执行日志与执行许可契约

## 背景

ADR 0017 的 durable control 保证指令交付：`control_delivered` 提交 user message 并移除 pending control。普通 prompt 不在控制日志中排队，delivered follow-up 也不再属于 pending，因此仅扫描 control 无法恢复已经开始的 Run。

当前本地 Runtime 的内存锁、控制 claim 和插件 lease 均不构成跨 Worker 的执行权。分布式写入需要独立许可，又不能给现有六方法 SessionStore 加一个可任选跳过的 fencing 参数。

## 决定

1. 新增 `execution_domain.py`，作为与 `domain.py` 同层的领域模块。它依赖已有消息引用等值对象，不依赖端口、运行时或驱动；不拆包、不修改 JSONL schema。
2. 普通 prompt 与 follow-up 都有独立的 RunSubmission 和 accepted 执行事实。Run 状态为 queued/running/completed/failed/cancelled，与 control 的交付状态分离。queued 可主动取消；接管保留 run_id、原 prompt、队列位置和预算。
3. 检查点为 before_prompt、awaiting_model、awaiting_tools、awaiting_boundary、terminal。完整 assistant 的 Finish 与引用必须同时记录；工具结果逐步移除未决调用。最终 assistant 后崩溃可从 boundary 完成，不重复推理。
4. 分别限制逻辑请求数、物理派发次数和恢复 attempt。每次派发先预留预算；接管清除旧的 in-flight reservation 标志，但保留已消耗派发次数。预算耗尽的孤立 Run 可用 ExhaustRun 在新许可下失败，不再增加 attempt。
5. Session 级 ExecutionAuthority 持久保留单调 epoch 和 active_run_id。许可匹配 session/owner/claim/epoch/purpose/run；有效期由提交时注入的数据库时间判断。Redis SessionLease 只是协调身份，不颁发最终 epoch。手动压缩是 compact-only maintenance 许可。
6. 心跳只更新协调状态，不追加执行日志，不增加 Session.version 或 Run.revision。续租不能复活已过期许可；比较释放保留 epoch 与中断 Run，只有匹配的终态提交才能清除 active_run。
7. `storage/execution_transitions.py` 通过纯函数生成 RunTransitionPlan 和 replay，失败不写入、不修改输入。RunEvent revision 与 Session sequence 分别计数。
8. OperationIdentity 识别稳定逻辑操作。同 ID、同请求返回原回执，即使后续状态已推进；同 ID、异请求拒绝。纯 Run journal 的摘要仅覆盖命令；未来执行存储的操作摘要必须覆盖完整命令、session mutation 和语义参数，不能直接用命令摘要代替完整事务去重。
9. `ports.py` 定义 SessionExecutionStore、ExecutionWriter、SessionLeaseManager。执行存储负责原子更新 Session 日志、投影、执行日志和去重记录；绑定 writer 持有不可变许可，每次写入重新校验权威状态。一次 commit_step 接受类型明确的 mutation 元组，以支持多条 steer 原子交付；无 Run 命令仅允许保留进度的压缩或控制 bookkeeping。维护许可不允许消息/控制写入，queued cancellation 走独立授权入口并与领取串行化。

## 状态转换

| 命令 | 前置条件 | 结果 |
| --- | --- | --- |
| RunSubmission | 日志为空，身份匹配 | queued / before_prompt |
| ClaimRun | queued 或非终态 running；新 epoch；attempt 有余量 | running；保留 checkpoint；attempt + 1 |
| CommitPrompt | running / before_prompt | awaiting_model；记录 prompt 引用 |
| DispatchModel | awaiting_model；派发预算有余量 | 派发计数 + 1；标记已预留 |
| CommitAssistant | awaiting_model 且已预留 | 有调用进入 awaiting_tools，否则 awaiting_boundary |
| CommitToolResults | awaiting_tools；全部 ID 属于未决集合 | 部分移除；最后一个结果进入 awaiting_boundary |
| ContinueRun | awaiting_boundary；工具续轮或已交付 steer；逻辑预算有余量 | awaiting_model；请求编号 + 1 |
| FinishRun completed | 无需续轮的 awaiting_boundary | completed / terminal |
| FinishRun failed/cancelled | running；queued 仅可 cancelled | 对应终态，禁止再执行 |
| ExhaustRun | 非终态；新 epoch；attempt 已耗尽 | failed；不增加 attempt |

共享函数只验证执行事实及引用形状。消息引用是否存在、实际 role/工具结果、Finish 与消息一致性、Session FIFO、submission_key 唯一、steer 接受窗口等必须由未来适配器在聚合事务内验证；纯 replay 不证明数据库事务或外部副作用成功。

## Definition Provider Consumer Lifecycle Evidence

Definition 为上述值对象与 ports 协议；Provider 在此切片是纯转换与 replay，测试使用显式 journal 替身。Consumer 是示例 50 与契约测试；真实 PG/Redis 和 Runtime Consumer 尚待后续步骤。纯模块无初始化、关闭或隐式注册；未来端口借用资源，资源所有者负责在心跳与执行全部停止后关闭。

证据为 `test_execution_contracts.py` 的状态矩阵、故障路径、取消、去重、预算和注入时间测试，以及示例的逐步 trace。测试不依赖付费模型、真实等待、系统 Temp 或异步事件循环。

## 备选方案

- 将 delivered 视作 completed：会遗漏正在执行的 Run，拒绝。
- 向 SessionStore 加可选 token：容易绕过保护并破坏旧替身，拒绝。
- Redis INCR 发放永久 fencing token：数据丢失或过期可能回退，最终 epoch 选择由 PG 维护。
- 把所有执行事实塞进 DurableSessionEvent：会扩大既有 schema/回放协议和本地实现，本阶段使用独立执行日志。
- 此时实现数据库和恢复 Worker：尚缺事务证据，先固定纯契约，再分步接入。

## 后果与适用范围

本 ADR 扩展 ADR 0017 的未来分布式恢复范围，不改变本地 AgentSession：本地已经 delivered 的 follow-up 仍不自动重跑。分布式模式将依据 Run journal 恢复，不能同时调用旧 pending-control 自动恢复路径。

此切片提供可持久化计划与端口，不提供真实存储、SQL 原子性、跨进程锁或自动恢复。新模块可直接导入用于实现契约；稳定应用 facade 的扩展随适配器/消费者落地评估，避免提前承诺未实现的组装行为。

工具执行结果未知时仍由现有尾部补偿处理，不自动重试外部副作用。旧 epoch 被拒绝只保护后续持久写入，不能撤销已经启动的工具。

## 验证方式

运行同步契约测试与示例 50；Ruff 和 strict mypy 覆盖新领域模块、纯状态机与端口。回归现有 Session replay、控制面和 Runtime 测试，确认旧六方法协议与 JSONL schema 未改。PG 事务、Redis 故障、真实双 Worker 和持久提交不确定性由后续集成步骤验收。
