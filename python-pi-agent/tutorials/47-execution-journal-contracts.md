# 第 47 章 Run 执行日志与可重放检查点

本章实现分布式会话规格的第一步：用正式包的纯值对象和转换函数表达可持久化执行事实。运行示例不需要 PostgreSQL、Redis、API key 或模型请求；它验证恢复决策，数据库事务留到后续步骤。

## 指令交付后为什么仍需 Run 日志

follow-up 的 `control_delivered` 将 user message 提交到模型历史，同时把 control 从 pending 中移除。模型可能还没开始请求，也可能正在执行工具。因此“交付完成”和“执行完成”必须是两个事实。

`RunSubmission` 保存普通 prompt 或 follow-up 的原始意图。`prepare_run_transition()` 返回一个 `RunEvent` 和新的 `RunRecord`。适配器应先把 event 与相关 Session 变化原子保存，再暴露新状态。纯函数返回成功不等于存储已经提交。

## 从检查点继续执行

运行仓库根目录下的命令：

```powershell
.\.venv\Scripts\python.exe python-pi-agent/examples/50_execution_journal.py
```

示例依次生成 accepted、claim、prompt、dispatch 和 assistant 事实。A 的最终 assistant 已经完整提交，但 Run 未标记完成；注入时间推进到租约边界后，B 取得新 epoch，记录新的 claim，然后从 awaiting_boundary 写 completed。最后输出派发次数仍为 1，并拒绝旧 owner 写入。

核心轨迹为：

```text
RunSubmission   queued   before_prompt      attempt=0 dispatches=0
ClaimRun        running  before_prompt      attempt=1 dispatches=0
CommitPrompt    running  awaiting_model     attempt=1 dispatches=0
DispatchModel   running  awaiting_model     attempt=1 dispatches=1
CommitAssistant running  awaiting_boundary  attempt=1 dispatches=1
ClaimRun        running  awaiting_boundary  attempt=2 dispatches=1
FinishRun       completed terminal          attempt=2 dispatches=1
old-owner-write=rejected
```

若 assistant 包含工具调用，则进入 awaiting_tools。每次 CommitToolResults 只移除对应未决 ID，缺失或重复结果被拒绝。工具全部闭合后仍需续轮；不能因为补偿完毕就把 Run 标记完成。真实补偿消息继续复用 AgentSession 现有机制。

## 执行权与心跳

`claim_execution()` 需要显式输入权威状态和 now；函数不会读取系统时钟，也不会操作 Redis。数据库适配器以后必须在 Session 行锁内加载最新状态并注入数据库时间。

到期边界使用 `expires_at <= now`。新的持有者推进 epoch；旧许可即使 owner 字符串相同，claim 或 epoch 不同也不能写入。续租改变 authority 中的有效期，不追加 RunEvent；过期后不能续租复活。

release 只清除匹配许可，保留 epoch 与中断 active_run。否则下一次轮询可能把尚未完成的 Run 当成不存在，先运行后面的任务。只有验证了匹配 Run 的终态，settle 才清除 active_run。

## 幂等与预算

操作去重必须发生在重新执行之前。纯日志重复 operation_id 且命令相同，返回当时的原回执；相同 ID 改变 prompt 等字段则拒绝。原回执可能早于最新状态，调用方需要当前进度时应显式 reload。

Run 命令摘要与未来事务请求摘要分开：执行存储还要覆盖 mutation、消息内容和语义参数。不能把“相同命令但不同工具结果”当成同一提交。

逻辑请求、物理派发、恢复尝试分别计数。网络派发前先记 DispatchModel；进程退出后这次预算仍然消耗。接管清除旧 reservation，但不会清零计数。再次派发需新的记录，达到上限后停止。ExhaustRun 允许在新 epoch 下把耗尽的孤立任务标记失败，而不获得另一次模型执行机会。

## 失败与取消练习

运行测试：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s python-pi-agent/tests -p test_execution_contracts.py -v
```

分别尝试：重复提交 prompt、没有派发预算就提交 assistant、提交不存在的 tool ID、用旧 epoch 写入、在 completed 后再次领取、在边界仍需工具续轮时 completed。每次失败都应保持原日志不变。

取消是显式 FinishRun(cancelled, reason) 事实；失租不等于用户取消，失租的 running Run 保留给新执行者。超时测试使用注入 now，测试没有真实 sleep。当前代码没有运行任务，因此本章验证取消状态，而不是证明 asyncio 取消传播；后者属于 Runtime 接线步骤。

## 本章边界与掌握标准

正式实现位于 `execution_domain.py`、`ports.py` 和 `storage/execution_transitions.py`。独立执行日志是本项目设计，不声称复制上游 Pi 的分布式 API；它复用已有事件重放、稳定身份和显式能力接缝。省略数据库、Redis 客户端、后台心跳、跨 Worker 结果订阅和实际恢复 Worker。

完成练习后，应能从命令序列判断恢复位置，解释 delivered 与 completed、Session.version 与 Run.revision、协调租约与执行权、返回计划与提交成功之间的区别，并指出哪些约束仍需数据库事务验证。架构决定见 [ADR 0049](../docs/adr/0049-execution-journal-contracts.md)，后续计划见[开发规格](../docs/design/phase-1-distributed-session-storage.md)。
