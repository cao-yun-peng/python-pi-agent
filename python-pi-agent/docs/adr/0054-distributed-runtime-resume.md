# ADR 0054 持久 Run 的 Runtime 接线与显式恢复

## 背景

ADR 0049–0053 已提供执行状态机、PG 原子检查点与回执、Redis 协调和受监督心跳。原 AgentSession 仍通过普通 SessionStore 写历史，本地 follow-up 恢复也不了解 Run journal。直接重用旧恢复路径会重复领取任务；重新调用 run(prompt) 会重写用户输入，并把请求序号与预算归零。

## 决定

- Builder 新增 with_distributed_execution，显式注入执行存储、租约端口、DistributedExecutionSettings 和可选时钟；执行存储与租约资源各有独立 owned/borrowed、cleanup。SessionStore 作为同一数据库/schema 的读取端显式提供，PG pool/Redis client 不通过 import 或默认工厂创建。
- 生产分布式路径要求读取端声明 supports_distributed_execution；默认 Memory/JSONL 不满足，装配时报错。离线测试使用明确声明的窄端口替身，不能成为生产降级。读取端与执行端指向同一数据源是应用组装契约，marker 不验证连接串或 schema 身份。
- 新增 DistributedExecution 值对象传递到 Runtime、AgentSession。未配置时原本地 controller、SessionStore、恢复和 Agent loop 行为保持原路径。
- SessionSpec 可声明成对的 recovery_spec_ref/hash，覆盖默认配置身份。选择不同于默认组件的分布式 SessionSpec 必须提供自己的身份；身份参与活跃 controller 匹配和 Run admission。恢复时若误用默认角色，在领取和模型派发前拒绝，而不是静默放宽工具或权限。
- _DistributedSessionControl 复用既有 FIFO dispatcher、RunHandle 和 idle barrier，替换 admission/恢复/取消/控制写入口。普通 prompt 和 follow-up 均先 submit_run，再将本地 future 入队。RunHandle 只负责本进程结果和实时事件；数据库 RunRecord 才是持久状态。
- 分布式打开 Session 只恢复插件，不运行旧 restore_pending_controls 的 follow-up 自动调度，不取消待恢复 Run 的 steer。已接受 Run 由显式 RuntimeSession.resume(run_id) 调度；候选扫描、配置重新组装和跨 Worker 调度仍属于阶段 7。
- 新增 ClaimRunExecutionRequest 与 claim_run：在 PG Session 锁内验证目标恰好是 active/FIFO 优先 Run，然后才递增 epoch/attempt。目标不符不消耗其他 Run 的恢复次数，不绕过队列。claim_next 保留原签名、请求摘要和行为，供后续候选调度使用；两个入口复用同一事务实现。
- _DistributedSessionExecution 取得 Redis/PG 许可后，在 LeaseHeartbeat 中执行；检查原 recovery_spec_ref/hash，不自动换成默认配置。配置引用与摘要由应用注册并维护，不保存密钥、对象序列化或自动推断的配置。内部 _resume_claimed 接受已绑定的 writer，不重新 submit、生成 run_id 或再次领取。
- _ExecutionSessionWriter 每次捕获当前 grant、绑定 writer，并用同一对象计算完整 operation 摘要。消息、Finish.reason、工具 ID、检查点与预算在同一个 PG step 提交。操作返回后才更新内存和 durable observer；提交未知时只查询原 operation_id，查不到就停止，不自动重跑外部行为。
- 初次 system prompt 与用户 prompt 同事务提交；follow-up 的 control_delivered 与 CommitPrompt 同事务。已有 prompt 从检查点恢复，不靠消息文本猜测是否提交。Run 完成后按 assistant 引用重建当时历史前缀，终态 resume 不领取、不启动 loop，也不重复发送完成事件。
- Agent loop 增加有限的 assistant committer、boundary、dispatch gate 和内部 _LoopResume 接缝，仍使用同一模型/工具循环。awaiting_model 从持久 request_number 请求；awaiting_boundary 从已提交 assistant/Finish 进入边界，不再推理或执行已提交工具列表。
- 在每次实际 agent-turn 模型派发前预留 DispatchModel。ModelDispatchHook、可选 DispatchBindableModelProvider 和独立绑定适配器将许可/预算检查带入 Direct/Resilient/Recording provider；绑定返回独立实例，不修改共享 provider。重试层的检查放在 retry/fallback 决策之外，预算拒绝不能被自定义重试策略重新解释。
- 原始 ModelProvider 的一次 stream 必须代表一次实际派发，SDK 自带重试由应用关闭；自定义重试/包装 provider 必须实现 bind_dispatch 并传递 hook。压缩模型也逐次检查租约，但不消耗 agent-turn 的请求/派发预算。ModelCompactionStrategy 支持绑定，普通压缩策略在 summarize 前检查许可；内部重试策略须传递绑定。
- 安全边界在 controller 锁内收集本 Run 的 durable steer，先领取控制，再将全部 delivery 与 ContinueRun 原子提交。无续轮需求时关闭本地接受窗口；PG finish_run 继续检查版本和已接受 steer，拒绝过时的完成决定。
- 成功、确定的业务错误或本地明确取消，通过停止心跳后的 finalize 提交 completed/failed/cancelled，再报告完成。失租、存储不确定性和父级强制中断只停止本 attempt，保留 running 检查点。RunHandle.abort 针对本地受管任务请求取消；没有许可时不伪造远端 Run 终态。
- 关闭期间某条队列取消写入失败时，该 Run 保留可恢复状态并向其本地 handle 报错；继续停止本地派发、取消并等待活动任务和心跳，然后再报告关闭错误，不能因队列 I/O 失败跳过清理。
- 工具恢复复用正式 interrupted_tool_result 与 find_unresolved_tool_calls。awaiting_tools 只补齐缺失结果；已经提交的结果保留，不自动重跑任何工具。新增 RepairToolTail 命令，仅允许新 Run 的 before_prompt 阶段关闭前一个终态留下的完整工具尾部；planner 要求全部未决 ID、recovery provenance、is_error 与明确的 unknown 结构，不能写成功工具结果。
- 自动压缩复用 Run grant；手动压缩先取得同一 Session 的维护许可并启动心跳，再复用同一 compactor。Redis 键丢失也不能越过有效 PG Run grant，丢许可的摘要不能覆盖旧历史。压缩和工具派发都有门，approval 返回后再次校验工具许可。

## 备选方案

再写一套 distributed Agent loop 会使工具验证、权限、流校验和压缩语义分叉；把 SQL/Redis 塞进 loop 会破坏端口方向，因此只注入通用接缝。仅在逻辑请求入口扣预算无法限制底层重试，选择实际派发前预留。

直接 claim_next 后发现目标不是用户指定 Run 再释放，会平白消耗前一个 Run 的 attempt；新增定向领取并在同一锁内拒绝。为已有 ClaimExecutionRequest 添加字段会改变旧摘要，因此采用独立请求类型。

## 后果

SessionStore 六方法、Session schema v6、既有 claim_next 摘要与 SQL 迁移 001/002 保持不变。Run journal v1 增加 RepairToolTail 标签，旧历史仍能读取；旧版 reader 不识别该标签，混合部署前必须先升级读取端。本步没有隐式迁移或生产基础设施部署。

终态消息和预算连续，但跨崩溃的精确 token 用量无法由检查点推算：AgentResult.usage 与实时 usage 仅描述当前 attempt，缓存终态结果不补造用量。恢复的逻辑序号、总派发次数与 attempt 来自持久记录。

不提供自动 RecoveryWorker、不自动跳过优先 Run、不把实时事件当成跨进程可靠通知，也不保证外部工具副作用 exactly-once。next-turn hook 在待请求续轮恢复时可能重新执行，应按其契约处理可重入行为。生命周期依赖端口协作取消；应用负责其注册配置、客户端与外部系统隔离。

## Definition / Provider / Consumer / Lifecycle / Evidence

Definition 是分布式配置值、定向领取、派发绑定与既有执行端口；Provider 是 PG/Redis/正式 provider 绑定和 writer；Consumer 是 Builder → AgentSession → 共用 dispatcher/loop。生命周期由 Runtime drain/cancel、LeaseHeartbeat join/cleanup、注册资源释放构成，插件沿用原 open/settled/close。

证据覆盖持久接收、prompt 去重、最终消息后恢复零推理、工具部分提交/未知补偿、续轮前崩溃、连续请求/派发预算、真实重试限制、steer/follow-up、维护压缩与失租、FIFO 定向领取、提交未知查询、完成通知顺序及资源关闭后零后台任务。测试均使用 FakeProvider，不调用付费模型。

## 验证方式

见[教程 52](../../tutorials/52-distributed-runtime-resume.md)的聚焦离线与显式 Redis/PG 命令，以及[示例 55](../../examples/55_distributed_runtime.py)。真实测试使用独立 schema 和随机 Redis 前缀；故障代理在真实 PG assistant 提交后中断，再用新 Runtime 恢复。默认测试不连接服务。

本地验证记录（2026-11-24）：Windows/Python 3.12，Redis 7/PostgreSQL 16 专用回环容器；相关离线测试共 322 项，268 项通过、54 项集成条件未启用而跳过。显式 PG/Redis、Runtime 和共享 Store 契约 69 项通过。Ruff 与 71 模块 strict mypy 通过；正式示例的正常/assistant 后中断模式通过。离线 wheel 检查包含新增 Runtime 模块、两个既有 SQL 资源、SessionSpec 恢复身份字段，以及禁用 redis/asyncpg 导入时仍可导入稳定 API。未调用付费模型；未声称本地 Python 3.12 验证替代 CI 多版本证据。
