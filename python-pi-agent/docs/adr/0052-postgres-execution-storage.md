# ADR 0052 PostgreSQL 执行日志与原子操作回执

## 背景

ADR 0049 固定 Run/检查点/执行权的纯契约，ADR 0051 已提供 PG 会话事件事务。现在需要把消息历史与执行进度一起保存，并解决旧执行者写入、重复请求和 COMMIT 回复丢失的窗口。只在客户端传 token 或只保存当前 Run 投影，均不能证明接管后的状态一致。

## 决定

- 新增 `PostgresExecutionStore` 实现 SessionExecutionStore，资源仍是借用外部 pool；绑定 writer 仅捕获不可变 ExecutionGrant，不产生连接或后台任务。
- 002 新增 session_runs、run_events、session_operations、Session 队列计数与 active_run 外键。001 保持原校验和。Run 投影保存完整强类型 record，生成索引列；加载时与独立执行日志 replay 比较。
- submit_run 为新 Session 创建 distributed 模式；拒绝隐式转换已有 local Session，避免接管一个仍在运行且不受许可约束的旧 controller。迁移管理不隐式修改执行模式。
- 统一锁 Session → Run → control。取得 Session 锁后单独读取数据库 clock_timestamp，校验当前 owner/claim/epoch/purpose/run/expiry。领取在 PG 内递增 epoch，优先恢复 active Run，再按稳定 queue_order 取 queued Run。没有进程级全局锁。
- SessionLease 是调用方协调身份。PG 检查其声明有效期并限制初次 grant 的期限，但不会连接 Redis 或把 nonce 当成可信证明；真实协调租约的取得/续期是后续 LeaseManager 的前置责任。PG 执行权本身仍独立保证写入互斥。
- 单次 step 使用纯 mutation planner 校验实际消息与 checkpoint 命令一致，再复用 prepare_* 和 Run 转换函数。Session 事件、control 投影、Run 事件、Run 投影及回执同事务提交。工具 ID、prompt、assistant 引用和 steer 交付必须与实际事实匹配。
- 新增强类型请求对象和 execution_operation。摘要由完整请求重新计算，包括类型标签、消息载荷、许可身份、预期版本；不能信任调用方给出的旧摘要。原命令摘要仍仅服务 Run journal，二者不混用。
- 幂等查找在有效期与版本判断之前，但在 Session 锁内；相同 key/内容返回原回执，不推进版本，不颁发新许可。不同内容、版本或方法重用同 key 拒绝。返回原许可不表示它仍有效，后续写入照常查询当前权限。
- 操作回执保存 Session 历史版本引用、当时 Run record 和 grant；通过 append-only 前缀重建原 SessionSnapshot。避免每次派发复制完整历史，同时支持进度推进后的原结果查询。
- 显式事务主体正常结束后，若 COMMIT/连接释放发生传输失联，带 operation 的写入抛 SessionCommitUnknownError，保留 session_id/operation_id。调用方查询 resolve_operation；查到回执才确认原提交，未查到不证明另一个事务失败，重试相同 key 仍须经过锁和幂等检查。
- renew/release 不追加日志、不改变 Session.version/Run.revision；过期许可不得复活，释放保留 epoch 与中断 active Run。它们没有 operation 参数，失联时调用方保守停止，不能当作可查询的业务提交。
- queued cancellation 与领取串行；失败/取消终态同时取消本 Run 尚未交付的控制。completed 必须处于最终边界且没有未处理控制。维护 grant 只允许 compact，不能提交消息或控制。

## 备选方案

仅比较调用方 request_digest 允许不同消息伪装成缓存重试；每次返回最新快照破坏“原结果”语义；只缓存消息文本不能重建压缩和控制；在回包丢失后自动重跑模型或工具会重复外部副作用。选择完整请求摘要、历史引用和显式查询确认。

继续复用 JSONL 做执行数据库不具备跨进程事务；此时实现 Redis/心跳/RecoveryWorker 则扩大阶段边界，保留到步骤 5–7。

## 后果

SessionStore 六方法、JSONL schema v6 与原本地运行路径不变；执行 API 独立导出。执行 codec 为 journal schema v1，拒绝未知字段/版本和损坏投影。阶段 4 已能持久接收、领取和提交 Run，但现有 AgentRuntime 尚不会自动使用它。

执行/回执读取仍可能 replay 全日志，优先验证一致性；日志、请求摘要和回执保留策略待后续阶段。执行存储是可信应用端口，身份授权、配置重建和秘密来源仍在应用层。

本阶段工具检查点支持 running Run 的原子工具结果/补偿提交，不实现恢复编排。取消/失败后遗留未知工具尾部如何在新 Run 前修复，须在 Runtime 接线/恢复阶段显式编排，不能重试外部工具或绕过许可。PG fencing 只能拒绝旧的 PG 写入，不能撤销已发出的工具副作用。

## Definition Provider Consumer Lifecycle Evidence

Definition 为 execution_domain、SessionExecutionStore/ExecutionWriter 与请求摘要契约；Provider 为 PG 执行适配器、纯 mutation planner 和 v1 codec；Consumer 为示例 53 与现有测试端口。生命周期是显式迁移、借用 pool 和事务归还；没有隐式注册、线程、后台续租或 Runtime 注入。

证据包括默认离线 codec/摘要/状态转换校验，真实 PG 并发领取与同 key 重试、旧 epoch、维护隔离、工具批次、steer 终态竞争、失败/取消原子回滚、001→002 保留数据升级，以及真实提交成功后故意丢失 COMMIT 回复的代理测试。示例使用 FakeProvider 和合成 SessionLease，不连接付费模型。

## 验证方式

按教程 50 运行聚焦离线测试；设置任务专用 PI_AGENT_POSTGRES_TEST_DSN 后执行 test_postgres_execution 与基础 Store 契约。pool/clock 由调用方或数据库提供，取消/锁测试通过数据库屏障定位，不依赖固定等待。默认测试不连接数据库，真实集成和 wheel SQL 包含测试显式执行。
