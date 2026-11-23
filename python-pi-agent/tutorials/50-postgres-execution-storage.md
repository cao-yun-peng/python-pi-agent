# 第 50 章 PostgreSQL 执行存储与幂等回执

阶段 3 保存模型历史，阶段 4 保存“这次 Run 执行到了哪里、谁能继续写、某个操作是否已经提交”。它实现独立的 SessionExecutionStore，不改变 AgentSession 的默认接线。

## 运行正式示例

安装 postgres extra，设置任务专用开发数据库，然后显式迁移并运行：

```powershell
$env:PI_AGENT_POSTGRES_DSN='postgresql://postgres@127.0.0.1:5432/pi_agent_dev'
.\.venv\Scripts\python.exe -m pi_agent_py.storage.postgres_cli migrate
.\.venv\Scripts\python.exe python-pi-agent/examples/53_postgres_execution.py
```

示例使用正式执行存储和 FakeProvider，创建一个独立 Session，依次输出 queued、领取、prompt、dispatch、assistant 和 completed。最后按 prompt 操作 ID 查询，会得到 version=1 的原快照，即使当前历史已经到 version=2。数据库仍需由调用方准备，DSN 不应写入版本控制。

示例中的 SessionLease 是合成边界替身；本章未实现 Redis，也没有后台心跳。真实租约管理在下一阶段提供。领取的最终 epoch 来自 PG，合成协调身份不会绕过当前 PG 持有者检查。

## 提交命令与消息必须一致

应用从 `pi_agent_py.api` 导入 PostgresExecutionStore、请求对象、命令和 execution_operation。构造 ExecutionStepRequest 后计算完整摘要，再调用绑定 writer。摘要覆盖实际消息和预期版本；如果只把命令摘要放进去，存储会拒绝请求。

CommitPrompt 必须指向本次真正提交的原始用户消息；follow-up 必须通过自己的 control_delivered 原子交付。CommitAssistant 的工具 ID 与实际 assistant.tool_calls 一致，只有预留 DispatchModel 后才能提交。CommitToolResults 逐项关闭未决调用，不能伪造没有提交的结果。

执行 API 调用前先复制请求中的可变 JSON 数据；缓存判断仍重新计算摘要。同 key 同内容返回当时结果；改变消息、许可或版本后复用同 key 会报 SessionValidationError。需要新的逻辑操作时生成新 operation_id。

## 执行权由数据库裁决

PG 在 Session 行锁内维护 epoch，并在锁取得后读取数据库时间。新领取恢复未完成 active Run，随后才运行 FIFO 队列。旧 epoch、错误 owner/claim、已过期许可或无许可的原始 Store 写入全部拒绝。

绑定 writer 捕获身份，不缓存“永久写权限”。续期后原 writer 可继续使用同一身份，但每次提交仍查看最新有效期；释放和接管会让旧 writer 失效。重复领取操作可能返回旧 grant 回执，调用方不能据此认定租约仍有效。

完成与控制接收也通过同一 Session 锁串行。若最终 assistant 后又接受了 steer，旧版本 finish 会冲突；最新版本仍需交付/取消 steer，不能悄悄完成。queued 取消则与领取竞争：已 running 的任务不能用无许可的 queued 取消入口停止。

## 提交回包丢失

SessionCommitUnknownError 表示事务主体已经完成，但未能确认 COMMIT 回复。捕获异常后读取它的 session_id 和 operation_id，通过 resolve_operation 确认结果，不能重新请求模型或工具来“重试”。

未查询到回执可能是未提交、尚在进行、无副作用领取空结果或读取失败，不能作为业务动作失败的证明。相同请求重试仍由 Session 锁和操作记录保证只提交一次。renew/release 没有业务 operation ID，失联时按失效策略停止工作，后续由数据库重新裁决。

测试中的 LostReplyPool 使用真实 PG：先让事务成功提交，再在返回客户端前抛连接异常。随后独立查询原回执、重试同 key，检查用户消息和 Run revision 都没有重复。

## 测试与练习

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p test_execution_codec.py -v
$env:PI_AGENT_POSTGRES_TEST_DSN=$env:PI_AGENT_POSTGRES_DSN
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p test_postgres_execution.py -v
```

默认离线测试检查格式、摘要和纯 mutation 校验。真实测试需要建 schema 权限，并清理自身生成的 schema；Windows 沙箱约束按 AGENTS.md 处理。观察十个 Worker 领取只有一个成功，再观察十次同 key 提交返回同一回执。篡改 message 却复用旧摘要、用旧 epoch 写入，以及强制中断消息插入，都应保持原状态或产生明确错误。

掌握标准：能区分 Session.version、Run.revision、epoch 与 operation_id；解释原回执与最新进度、租约协调与执行权、事务原子性与外部副作用的区别；能指出 Redis、Runtime 注入、失败尾部恢复编排和自动接管尚未实现。与 Pi/DeepSeek Harness 的映射仍为既有事件与能力接缝，PG 执行日志为本项目设计。见 ADR 0052。
