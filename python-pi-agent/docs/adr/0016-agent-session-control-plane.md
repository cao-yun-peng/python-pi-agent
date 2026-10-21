# ADR 0016: AgentSession Control Plane

## 背景

`asyncio.Lock` 只能防止两个 run 同时修改会话，不能表达活动 run、steer 优先队列、follow-up FIFO、主动 abort、run settled 和 session idle。把并发 `run()` 隐式阻塞在锁上也无法观察或清理尚未开始的请求。

## 决定

- `AgentSession` 使用显式 dispatcher 串行消费 `_QueuedSessionRun`。
- 控制状态为 `idle | running | aborting | settled`。
- `run()` 入队并等待自己的结果；并发调用保持 FIFO。
- `follow_up()` 入队后立即返回 run ID，由 dispatcher 在前序 run settled 后执行。
- `steer()` 只在活动 run 接收窗口开放时成功；消息在当前模型/工具 turn 完成后的安全边界提交，并触发下一模型请求。
- 最终 turn 检查空 steer 队列和关闭接收窗口使用同一控制锁，保证已接受的 steer 不会丢失。
- `abort(clear_queue=True)` 默认取消活动任务、清空 steer 和尚未开始的 follow-up，并等待 `idle`。
- `wait_until_idle()` 是 session 级稳定屏障；`run_settled` 继续表示单个 run 的 finally 事件。

## 备选方案

- 继续依靠互斥锁：无法区分排队、运行和停止，也无法取消指定的等待请求。
- steer 直接取消 Provider：会丢失部分流并混淆 steer 与 abort。
- steer 任意时刻直接写 history：可能插进 assistant tool call 与 tool result 之间，破坏 Provider 协议。

## 后果

run 的调用者取消时，会取消自己的活动或排队请求；follow-up 的后台失败由 EventSink/durable history 观察，不产生 asyncio 未取异常警告。当前控制队列是进程内状态，进程退出前尚未消费的队列不会恢复；消费后的消息按照普通 durable message 恢复。

## 验证

- Provider 阻塞时 `abort()` 传播取消并最终进入 idle。
- follow-up 在前序 run 后按 FIFO 自动执行。
- 工具阻塞时接受 steer，工具完成后按 `tool result -> steer -> next model request` 排序。
- idle 状态拒绝 steer。
- 默认 abort 清空未开始的 follow-up。
