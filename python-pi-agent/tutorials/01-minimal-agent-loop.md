# 第一章：亲手实现最小 Agent Loop

## 这一章解决什么问题

LLM SDK 通常只负责“一次请求”。Agent Harness 还要负责维护消息历史、消费流、判断响应是否完整、发布运行事件，以及在失败或取消后留下可解释的状态。

第一阶段的数据流是：

```text
prompt
  -> commit user message
  -> ModelProvider.stream(messages)
  -> TextDelta...
  -> Finish
  -> commit assistant message
  -> settled
```

入口在 `Agent.run()`。先读测试，再读实现：

- `tests/test_minimal_loop.py`
- `src/pi_agent_py/runtime.py`

## 三个最重要的设计决定

### 1. Provider 返回规范化事件

核心循环不认识 OpenAI、Anthropic 或 DeepSeek 的 SDK 对象，只认识 `TextDelta` 和 `Finish`。未来每个供应商适配器负责把自己的响应转换成这些领域事件。

这就是端口的价值：更换模型只替换 Provider，不重写 Agent Loop。

### 2. 部分输出不是持久事实

`TextDelta` 属于 live event，可以立刻展示在终端或网页上。但只有收到 `Finish` 后，完整 assistant message 才作为 durable event 提交。

如果供应商中途失败，用户消息仍然存在，而不完整的 assistant 文本不会污染下一次模型上下文。这条边界将直接影响会话恢复、重试和审计。

### 3. settled 不等于 success

成功、失败和取消都会进入 `run_settled`。应用层可以把它理解为“这次 run 已经没有后续后台工作”，而不是“模型成功回答”。

因此：

- `run_completed` 表示成功。
- `run_failed` 表示可分类的失败。
- `run_cancelled` 表示调用者主动终止。
- `run_settled` 表示生命周期真正结束。

## 为什么从 FakeProvider 开始

真实 API 会引入网络、费用、限流、模型随机性和密钥管理，让我们无法判断失败究竟来自循环还是供应商。`FakeProvider` 用脚本表达模型行为：

```python
FakeProvider([
    [TextDelta("你"), TextDelta("好"), Finish()]
])
```

它还可以注入 `ProviderError`，或用 `WaitUntilCancelled` 停在流中验证取消传播。

## 动手练习

1. 运行示例，画出事件顺序。
2. 删除脚本里的 `Finish()`，观察协议错误和消息历史。
3. 增加第二个 scripted response，连续调用两次 `agent.run()`，检查第二次 Provider 收到的历史。
4. 思考：如果收到 `Finish` 后又收到 `TextDelta`，为什么必须报错？

## 当前主动省略的复杂度

- 工具调用与多轮 tool result
- 会话存储和 durable event log
- 供应商重试与退避
- token 计数和上下文压缩
- 插件 Hook 与权限审批
- 后台事件队列和遥测

这些会在后续章节逐层加入，不会提前塞进最小循环。
