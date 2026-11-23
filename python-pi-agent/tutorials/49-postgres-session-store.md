# 第 49 章 PostgreSQL 六方法会话存储

本章把已有 SessionStore 协议落实为真实事务适配器：共享 Codec 决定事件内容，transitions 决定合法变化，PostgreSQL 决定并发写入是否提交。AgentSession 不需要知道数据库 SQL。

## 安装与显式迁移

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\python-pi-agent[postgres]"
$env:PI_AGENT_POSTGRES_DSN='postgresql://postgres@127.0.0.1:5432/pi_agent_dev'
.\.venv\Scripts\python.exe -m pi_agent_py.storage.postgres_cli migrate
.\.venv\Scripts\python.exe python-pi-agent/examples/52_postgres_sessions.py
```

DSN 必须指向自己准备的开发数据库，上例无密码仅适用于已配置本地认证的实例。也可显式给示例加 --migrate；Store 构造与读写从不自动迁移。真实凭据从环境或应用秘密来源注入，不写入示例、日志或命令行参数。

示例用 FakeProvider 完成 user/assistant 提交，重新构造 Store 读取历史，并展示过时 version 被拒绝。再次运行时 before_version 应等于上一次持久化版本，事件 sequence 连续。CLI 第二次迁移输出 applied_migrations=none。

应用从 `pi_agent_py.api` 导入 PostgresSessionStore、PostgresPool 和 PostgresSettings。pool 在应用入口创建，按所需 schema 配置 search_path，生命周期由应用拥有。Settings 控制连接取得、锁与 SQL 超时；Store 不关闭借用 pool。导入正式包不要求安装 asyncpg，只有实际连接时才需要 postgres extra。

## 写入事务

新写入先幂等建立 Session 行，再锁定聚合根并读取完整日志。prepare_session_append/compaction/control 生成事件计划后，适配器原子插入事件、同步控制投影并推进 version。多个客户端用相同 expected_version 提交，只能有一个成功；失败者不能重新执行模型或工具来“重试存储”。

锁顺序以 Session 为锚点，无进程级 Python 锁。fork 使用 repeatable-read 读取源快照，目标插入与复制事件、投影同事务完成。session_controls 复合主键允许源与分支保留相同 control_id；独立 append 不影响源历史。

事件 payload 是完整 schema v6 envelope，索引字段由 generated columns 从它派生，避免双份数据漂移。load/read_events 在同一只读快照中核对 metadata/version 和 compaction 指针；首次读未知 ID 不建空记录。第一版不裁剪历史，也不从 compaction 直接跳过前面的控制事实。

## 迁移与错误

迁移文件与数据库迁移版本分开于事件 schema_version。迁移器先取得 advisory transaction lock，校验已应用版本和 SQL checksum，再执行缺少的版本。SQL 失败时 DDL 与版本标记全部回滚。修改已发布 001 会触发 checksum 拒绝；后续更改必须新增 002 等迁移。

冲突映射 SessionConflictError；损坏历史/投影为 SessionReplayError；无效 JSON 或 schema 输入为 SessionValidationError；连接、驱动和锁超时为 SessionStorageError，异常链保留。取消不包装成存储失败。提交可能成功但回包丢失时不能盲目重试；操作去重由后续执行存储实现。

## 确定性契约与真实验证

默认测试无需数据库或驱动：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p test_postgres_session_store.py -v
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p test_session_store_contracts.py -v
```

真实测试必须显式给出任务专用测试数据库：

```powershell
$env:PI_AGENT_POSTGRES_TEST_DSN=$env:PI_AGENT_POSTGRES_DSN
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p test_postgres_session_store.py -v
```

测试为每个用例生成独立 schema 并在结束时删除，调用方需要建 schema 权限。Windows 受限沙箱下按 AGENTS.md 直接在获准的沙箱外离线运行 asyncio/Temp 测试。默认没有 DSN 时集成用例跳过，边界错误替身仍运行。

观察十次竞争的一个成功与九个 conflict，再观察触发器令第二条事件失败时第一条和新 Session 一起回滚。取消测试用数据库锁等待作为屏障，不用固定 sleep 猜测进度。迁移并发、checksum 篡改和失败 DDL 测试解释“能建表”和“可安全演进”的区别。

## 本章边界

本次只支持 local 会话执行写入。distributed 标记会拒绝无许可写与直接分叉；预留 epoch 字段不代表已有 fencing 或自动接管能力。执行 Run 表和 PG active_run 外键在后续迁移加入，Redis 与恢复 Worker 继续按开发规格实现。

与固定 Pi 的映射仍是 Session 事件与历史恢复概念，不承诺上游 SQL API 等价；DeepSeek Harness 对应显式 Provider/Consumer/Lifecycle 接缝。掌握标准是能解释版本/主键/行锁的不同责任、还原 rollback trace，以及指出数据库日志保护不了工具外部副作用。详见 ADR 0051。

底层机制参考：[asyncpg API](https://magicstack.github.io/asyncpg/current/api/index.html)、[PostgreSQL 15 显式锁](https://www.postgresql.org/docs/15/explicit-locking.html)。生成列和完整 v6 envelope 的组合为本项目设计。
