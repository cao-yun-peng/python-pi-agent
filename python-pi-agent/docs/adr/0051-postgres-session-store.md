# ADR 0051 PostgreSQL 基础会话存储与显式迁移

## 背景

JSONL schema v6 已抽取共享 Codec，但文件实例锁无法协调多个进程。需要一个支持数据库事务和乐观并发的 SessionStore，作为分布式执行日志的持久基础，同时保持本地 Memory/JSONL 与离线评测可用。

## 决定

- PostgresSessionStore 实现原六方法协议。构造时仅保存调用方注入的 asyncpg-compatible pool 和强类型 PostgresSettings，不连接、不迁移、不关闭借用 pool。
- 使用 PG 15+，本次真实验证 PG 16；可选 extra 为 postgres，只有 CLI/示例才显式加载 asyncpg。稳定 facade 导出 Store、Settings 和窄化 Pool 协议。
- append/compact/record_control 在 Session 行锁下调用已有 prepare_*。会话版本、事件与控制投影同事务提交；唯一约束为物理兜底。没有全局 Python 锁或自动行为重试。
- load/read_events 采用只读 repeatable-read 事务；fork 采用可写 repeatable-read，复制源的一致前缀。所有读取校验元数据版本及 compaction 指针，未知 Session 不建行。
- session_events.payload 保存完整共享 v6 envelope。event_id/kind/run/turn/schema/provenance 使用 generated columns，数据库不会出现两份独立维护的事实。主键 session/sequence，并核对 JSON 中的 session/sequence。
- 控制投影使用 (session_id,control_id) 主键，fork 重放全部控制事件以重建投影。共享 replay 允许终态后复用 control_id，投影 enqueue 相应 UPSERT 重置 claim/取消字段。更新缺失投影视为 ReplayError 并回滚。
- 001 只创建 sessions、events、controls。为 execution_mode、epoch 和许可身份预留字段；Run journal、active_run 外键及协调能力留到后续版本迁移，避免提前创建未实现的数据面。
- 无许可写仅允许 local 会话；distributed 行可读，但 append/compact/record_control 与直接 fork 被明确拒绝。后续执行 writer 不得通过公共原始 Store 绕过许可。
- 迁移在 DB advisory transaction lock 内校验连续版本、文件名和 SHA-256，再同事务执行 DDL 与版本记录。checksum 以 UTF-8/LF SQL 计算，安装包携带 SQL。未知数据库版本、缺口或 checksum 改变均拒绝。migrate CLI 是显式管理操作，不接收模型 SQL。
- acquire/lock/statement 超时可配置；所有 SQL 用参数绑定。已知 Session 唯一约束冲突映射 ConflictError，其余驱动/连接/超时异常映射 StorageError；JSONB 表示错误映射 ValidationError，领域 Replay/Validation 保持不变，CancelledError 传播并退出事务。

## 备选方案

以应用锁替代数据库事务不能跨进程；按供应商 JSON 重新编码会漂移；完整 JSON envelope 加手工索引列会重复维护事实；使用 generated columns 让共享 Codec 与数据库索引各司其职。直接预建全部 Run 表会提前冻结下一阶段尚未实现的事务契约，选择逐版本迁移。

## 后果

此阶段交付可用的基础存储，不构成分布式 Run 自动接管。PG 租约/Redis、后台 worker、执行 grant 写入和操作去重仍未实现。

每次写操作仍完整读取/replay，长历史下持锁成本较高；此阶段优先可验证性，不做增量快照。pool 必须按应用指定 schema/search_path 组装；多个 schema 的迁移在同一 DB advisory key 下保守串行。

JSONL 原有行为不改；PG JSONB 不支持非有限数值或 NUL 等部分 JSON 表示，PG 边界明确拒绝这些输入。事务提交回包丢失时六方法接口只能返回 StorageError 并保留原因，不能自动重试；稳定 operation_id 查询确认由 Step 4 执行存储提供。

## Definition Provider Consumer Lifecycle Evidence

Definition 为现有 SessionStore、共享 v6 codec 和窄化 pool 协议。Provider 为 PostgresSessionStore 与显式迁移器；Consumer 是 AgentSession、示例 52 和共享契约测试。应用 owns pool，Store borrows；构造无 I/O、迁移显式、事务退出回滚/归还连接，CLI 自行关闭所拥有资源。

默认离线 Evidence 包括错误替身和 Memory/JSONL 共享契约；显式 PG 测试用独立 schema、两个 pool 验证同版本竞争、重开、批量失败、投影失败、锁超时、取消、重复/并发迁移与 checksum/未知版本拒绝。构建 wheel 验证 SQL 包含与加载。示例使用 FakeProvider，不调用模型 API。

## 验证方式

按教程 49 执行默认聚焦回归，再设置 PI_AGENT_POSTGRES_TEST_DSN 启用真实数据库测试。仅使用任务专用数据库，测试创建并删除自身生成的 schema。迁移与数据删除权限不属于模型工具权限；生产应用应限制数据库账户并自行管理备份与保留期。
