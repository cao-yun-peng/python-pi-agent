# 第十六章：AgentSession 控制状态机

## 1. 为什么锁不等于控制面

旧实现用 `asyncio.Lock` 串行化 `run()`。第二个调用虽然会等待，但系统不知道它是 follow-up、无法显示队列、不能在开始前取消，也不能判断 session 是否真正 idle。

新状态机为：

```text
idle -> running -> settled -> idle
           |           |
           | abort     +-> running（还有 follow-up）
           v
        aborting -> settled
```

`run_settled` 是一个 run 的终止屏障；`wait_until_idle()` 要求活动 run 和 follow-up 队列都为空。

## 2. 四个公开控制动作

```python
result = await session.run("立即执行并等待结果")
run_id = await session.follow_up("排在已有工作之后")
await session.steer("当前任务下一轮先看到这句话")
await session.abort()          # 默认清队列并等到 idle
await session.wait_until_idle()
```

`steer` 不取消当前 Provider 或工具。它只在安全边界提交：

```text
assistant(tool_calls) committed
  -> tool results committed
  -> accepted steer messages committed
  -> NextTurnHook / compaction
  -> next provider request
```

如果当前 assistant 已准备成为最终回答，Agent 会在同一控制锁内检查 steer 并关闭接收窗口。因此 `steer()` 要么成功且必定被消费，要么明确抛出 `SessionControlError`。

## 3. Follow-up 与并发 run

follow-up 是新的 run，拥有新的 `run_id`。dispatcher 按 FIFO 执行；当前 run settled 后，如果队列非空，session 直接回到 running，不会先报告 idle。并发调用 `run()` 也进入相同队列，但每个调用者等待自己的结果。

控制队列当前是进程内状态。尚未消费时进程退出会丢失；一旦 steer/follow-up 被提交为 user message，就进入 durable log。这一取舍避免在当前阶段引入 durable queue claim/ack/lease 状态机。

## 4. Abort 和调用者取消

`abort()` 取消活动 Agent task，取消信号继续传播到 Provider、Sandbox 和 Tool。默认还会取消所有未开始 follow-up，并等待 session idle。若使用 `clear_queue=False`，只等待当前活动 task 结束，dispatcher 可以继续处理剩余队列。

直接取消 `asyncio.create_task(session.run(...))` 也会撤销该提交：尚未开始则从队列移除，已经开始则取消活动 task。已经写入 durable log 的消息不会回滚；下一次 run 会使用未决工具恢复逻辑修复尾部。

## 5. 运行示例

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\16_session_control.py
```

测试覆盖 Provider 阻塞 abort、工具阻塞 steer、FIFO follow-up、idle 拒绝 steer，以及 abort 清理队列。

## 6. 与 Pi 的映射

Pi 提供 `abort()`、`steer()`、`followUp()` 和 `agent_settled`。本项目保留核心语义，但明确区分 per-run settled 与 session idle，并暂时省略图片队列、持久化控制队列、retry queue、TUI editor 恢复和跨进程控制。
