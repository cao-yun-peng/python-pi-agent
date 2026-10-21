# 第 4 章：会话存储与 AgentSession

这一章解决一个具体问题：新的 Agent run 怎样找回之前的消息，并在失败或取消后保留已经提交的状态。

## 1. 与前面三章的关系

第 1 章中的 `Agent` 已经能维护一次进程内的消息历史；第 3 章又加入了 assistant tool call 和 tool result。此前这些消息都只存在于 `Agent._messages`：

```text
user → assistant(tool call) → tool(result) → assistant
```

只要重新创建 `Agent` 或重启进程，这段上下文就无法恢复。本章增加的不是另一套 Agent loop，而是在已有循环外增加会话编排和存储边界。

## 2. 四个组成部分

### SessionSnapshot

`SessionSnapshot` 是某个时间点的不可变视图：

```python
SessionSnapshot(
    session_id="learning-session",
    version=3,
    messages=(system_message, user_message, assistant_message),
)
```

它回答三个问题：这是哪个会话、读取时是什么版本、模型可见历史是什么。

### SessionStore

`SessionStore` 是 runtime 依赖的端口：

```python
class SessionStore(Protocol):
    async def load(self, session_id: str) -> SessionSnapshot: ...

    async def append(
        self,
        session_id: str,
        expected_version: int,
        messages: Sequence[Message],
    ) -> SessionSnapshot: ...
```

runtime 不知道实现使用字典、JSONL、SQLite 还是远程数据库。

### InMemorySessionStore

`InMemorySessionStore` 是第一份确定性实现。内部使用可变 list 追加 durable events，对外返回 tuple 日志和 tuple 消息快照，防止调用者绕过版本检查修改历史。

### AgentSession

`AgentSession` 负责组装：

```text
load snapshot
→ 创建携带历史的 Agent
→ Agent 执行模型与工具循环
→ 每条完整消息写回 SessionStore
→ 返回最新快照中的消息
```

## 3. 为什么需要 expected_version

两个请求可能同时读到 version 2：

```text
A: load → version 2
B: load → version 2
A: append(expected=2) → version 3
B: append(expected=2) → conflict
```

如果没有比较版本，B 可能基于过期上下文继续写入。当前版本表示 replay 已消费的最后事件序号；数据库实现必须在一个事务中完成“比较版本并追加”。

## 4. 为什么逐消息提交

整轮结束后批量保存看起来简单：

```text
Agent.run 成功 → 保存本轮全部消息
```

但 Provider 失败或任务取消时，`run()` 不会返回，用户消息也会丢失。因此 `_commit()` 的顺序是：

```text
await SessionStore.append
→ 加入 Agent 内存
→ 发出 durable message_committed event
```

只有存储成功后，系统才声明消息已经提交。流式 token 仍然只是 live event；没有收到完整 `Finish` 的 assistant 文本不会写入模型历史。

## 5. 运行续聊示例

在仓库根目录执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
\.venv\Scripts\python.exe .\python-pi-agent\examples\04_session_resume.py
```

示例在两轮之间重新创建 `AgentSession`，但继续使用相同的 `session_id` 和 `SessionStore`。最终历史应为：

```text
system
user: 我叫小明。
assistant: 你好，小明。
user: 我叫什么？
assistant: 你叫小明。
```

第二轮能够回答，不是因为旧 `AgentSession` 仍在内存中，而是因为新对象从 store 加载了前三条消息。

## 6. 失败与取消语义

如果用户消息已经持久化，随后 Provider 失败：

```text
stored: user("remember this")
not stored: assistant message
```

取消遵循相同原则：已完成提交的消息保留，尚未形成完整消息的流式增量不进入 durable history。这为后续的“重试最后一条未回答用户消息”提供真实状态。

工具调用还需要额外处理。assistant tool-call message 会在工具开始前提交，因此取消可能留下：

```text
user
assistant(tool call A, tool call B)
tool result A
<cancelled before result B was committed>
```

如果直接追加下一条 user message，Provider 会看到不完整的工具协议。`AgentSession` 因此在下一次 `run()` 或 `compact()` 前调用纯函数 `find_unresolved_tool_calls()` 校验历史。它只修复位于会话尾部的未决批次：已经存在的结果保持不变，每个缺失结果追加一个持久化错误 tool message，内容明确说明外部副作用结果是 `unknown`，不能假定失败或自动重试。恢复后才提交新的用户输入。

```text
load snapshot
→ validate tool call/result batches
→ append recovered error result B
→ emit tool_call_recovered
→ append new user message
→ call provider
```

如果未决调用后已经出现了新的 user/assistant 消息，append-only store 已经无法把结果插回正确位置。此时系统抛出 `SessionValidationError`，要求显式修复日志，而不是静默制造顺序错误。

当前恢复只能确认“没有 durable result”，不能确认外部工具是否已经产生副作用。这正是恢复结果使用 `outcome=unknown` 的原因。后续加入 durable `tool_execution_started`、幂等键和 `retry_safe` 策略后，才能进一步区分“从未开始”和“可能已经执行”。

## 7. 与 Pi 和 DeepSeek Harness 的映射

- `AgentSession` 对应 Pi 中 session 对 Agent loop 和消息状态的编排职责；截至本章尚未加入 queue、steer、fork 和 compaction，后续章节会逐步补齐其中一部分。
- `SessionStore` 使用 DeepSeek Harness 风格的 capability seam：Definition 是端口和快照，Provider 是内存实现，Consumer 是 `AgentSession`，Evidence 是测试与示例。
- 当前内存 Provider 已使用事件日志作为事实来源；仍主动省略数据库、跨进程锁和迁移机制，先把加载、提交、失败和取消语义讲清楚。

## 8. 掌握标准

完成本章后，应能独立解释：

1. 为什么 `SessionSnapshot.messages` 使用 tuple。
2. 为什么同一对象的 `asyncio.Lock` 不能代替 `expected_version`。
3. 为什么消息必须先写存储，再发 `message_committed`。
4. 为什么 tool result 必须连同 `tool_call_id` 一起恢复。
5. 为什么流式 token 不应直接成为 durable message。

下一章会拆开事件追加与 replay，解释 `SessionSnapshot` 为什么只是日志投影。
