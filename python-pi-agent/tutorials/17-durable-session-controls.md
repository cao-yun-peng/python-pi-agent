# 第 17 章：Steer 与 Follow-up 的持久化恢复

## 原来的问题

第 16 章建立了 `abort`、`steer`、`follow_up`、`settled` 和 `idle`，但只把待处理输入放在内存 `deque`。调用方拿到 follow-up run ID 后，如果进程在执行前退出，该输入会永久丢失。

只把 prompt 写入日志也不够。若“提交 user message”和“标记队列已消费”是两个 append，进程可能恰好在两者之间崩溃，恢复后重复投递同一条消息。

## Durable transition

每个控制意图使用以下状态转换：

```text
control_enqueued
      |
      v
control_claimed -- lease expires --> 可重新 claim
      |
      +--> control_delivered
      |
      +--> control_cancelled
```

`control_delivered` 同时携带 user message。Replay 在应用一个 durable fact 时完成“提交消息 + 删除 pending control”，因此不会出现半提交窗口。

## Steer 与 follow-up 为什么不同

- `steer` 绑定正在执行的目标 run，只能在该 run 的下一个安全 turn 边界交付；重启后目标 run 已不存在，所以恢复逻辑写入 `control_cancelled`。
- `follow_up` 预留自己的 run ID，在当前工作 settled 后执行；重启后仍有意义，所以未 claim 或 lease 已过期的记录会重新进入 FIFO dispatcher。

这避免了把旧任务的 steer 错投给一个无关的新任务。

## Claim lease

dispatcher 在执行 follow-up 前写 `control_claimed`，其中包含 owner、claim ID 和过期时间。恢复者在 lease 有效时不会抢占；lease 过期后可以重新 claim。

这提供的是 durable 消费协调，不是对外部副作用的 exactly-once 保证。若工具调用已经影响外部系统而结果尚未持久化，仍需工具自身的幂等键或补偿策略。

## 恢复示例

运行：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\17_durable_controls.py
```

示例先用一个 `JsonlSessionStore` 写 `control_enqueued`，再创建新的 Store 和 AgentSession 模拟进程重启。输出应包含：

```text
control_enqueued -> control_claimed -> control_delivered -> message_committed
```

最终 `pending_controls` 为空，消息历史包含恢复出的 follow-up 和模型回复。

## 失败与取消

- abort 在删除内存队列前写 `control_cancelled`；
- 恢复时孤立 steer 写 `control_cancelled`；
- claim lease 未过期时恢复者保持等待，不重复消费；
- replay 遇到未知 control、重复终态或非法顺序会抛出 `SessionReplayError`；
- Provider 取消不会抹掉已经 durable 提交的 user message。

确定性测试位于 `tests/test_session_control.py`、`tests/test_session_log.py` 和 `tests/test_jsonl_session_store.py`。

## 与 Pi 的对应关系

Pi 的 steer/follow-up 队列主要服务当前进程的交互体验。本项目增加 durable transition、claim lease 和 replay，是为了明确 crash recovery 教学语义。当前 JSONL Store 仍只有实例内锁，不承诺多个 OS 进程安全争抢同一文件；生产实现应使用数据库事务或跨进程文件锁。
