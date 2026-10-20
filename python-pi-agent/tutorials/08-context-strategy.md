# 第 8 章：ContextStrategy 接缝

这一章解决的问题是：会话历史越来越长时，我们应该在哪里决定“下一次模型请求能看到哪些消息”，同时不破坏可回放的完整历史。

本章只建立接缝，不实现 token 估算、裁剪或摘要压缩。

## 1. 两份看起来相似但语义不同的数据

```text
SessionStore / Agent.messages
    完整、已提交、可回放的 durable history
                 |
                 v
          ContextStrategy.prepare()
                 |
                 v
         ContextPlan.messages
    仅供这一次 ModelProvider 请求使用的投影
```

`Agent.messages` 是事实，不能为了节省 token 而删除。`ContextPlan.messages` 是一次请求的视图，将来可以按预算选择消息或加入摘要。模型请求结束后，新的完整 assistant message 仍提交到前者。

这和前面会话章节的关系非常直接：`SessionStore` 负责保存事实，`ContextStrategy` 负责读取事实后生成临时视图。

## 2. 四个角色

### ContextPlan：结果对象

`ContextPlan` 包含选出的消息、源消息数量、省略数量，以及尚未启用的 token 预算和估算字段。它使用 tuple 保存消息，避免调用方改变容器本身。

### ContextStrategy：端口

```python
class ContextStrategy(Protocol):
    async def prepare(self, messages: Sequence[Message]) -> ContextPlan:
        ...
```

端口是异步的，因为后续策略可能加载资源或调用压缩器，也因此能继承 Agent run 的取消信号。策略不得修改传入的 committed history。

### FullHistoryContextStrategy：默认 Provider

默认策略把输入转换为 tuple 并全部返回。因此加入接缝后，现有 Agent 的模型可见行为不变。

### Agent：Consumer

每次 `_request_model()` 都先等待策略生成计划，再发出 `context_prepared` capability event，最后才发出 `model_request_started` 并调用 Provider。工具调用后的第二轮模型请求也会重新准备上下文，而不是复用旧计划。

## 3. 一次请求的执行顺序

```text
提交 user message（durable）
        -> ContextStrategy.prepare(full history)
        -> context_prepared（capability）
        -> model_request_started（live）
        -> ModelProvider.stream(ContextPlan.messages)
        -> 提交完整 assistant message（durable）
```

`context_prepared` 只记录数量和预算元数据，不把完整提示词复制进观察日志，避免日志膨胀和敏感内容重复暴露。

## 4. 失败与取消

- 策略抛出的普通异常会转换为 `ContextPreparationError`，不会被误报为 Provider 故障。
- 策略失败时，已经提交的 user message仍留在 durable history 中，但 Provider 不会被调用。
- `asyncio.CancelledError` 不包装，继续传到 Agent run；run 发出 `run_cancelled`，最后一定发出 `run_settled`。

这延续了前面 Agent loop 的规则：取消是一条控制流，不是普通失败。

## 5. 运行示例

在仓库根目录执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe .\python-pi-agent\examples\08_context_strategy.py
```

你会看到 Provider 收到前三条已提交消息，而最终 durable history 还多出随后提交的 assistant 回答。trace 中会显示 `source_message_count=3`、`selected_message_count=3` 和 `omitted_message_count=0`。

## 6. 与 Pi / DeepSeek Harness 的映射

在本仓库固定的 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 中，`packages/agent/src/agent-loop.ts` 的 `runLoop()` 会在模型调用前执行可选的 `transformContext`。Pi 的 coding agent 还在 compaction 前后提供扩展 Hook、分支摘要和 provider 特定转换。本项目当前只保留最小核心：完整历史进入一个可替换端口，端口返回显式计划。

对应 DeepSeek Harness 的 capability 思路：

- Definition：`ContextPlan` 与 `ContextStrategy`。
- Provider：`FullHistoryContextStrategy`。
- Consumer：`Agent._request_model()`。
- Lifecycle：策略随 `Agent` 显式注入，没有全局注册或后台资源。
- Evidence：`test_context_strategy.py` 验证投影、失败、取消和 `AgentSession` 持久化边界。

主动省略：tokenizer、预算计算、消息组完整性规则、摘要模型调用、压缩 durable event 和缓存。下一小步应先实现 token budget 与不可拆分消息组，再讨论真正的 compaction。

## 7. 掌握标准

完成本章后，应能独立解释：

1. 为什么裁剪模型输入不能等同于删除 session history。
2. 为什么每次工具续写前都要重新运行 ContextStrategy。
3. 为什么 `context_prepared` 属于 capability event，而不是 durable event。
4. 为什么策略异常不能包装成 ProviderError。
5. 如何写一个新的策略，而不修改 Agent loop、SessionStore 或 ModelProvider。
