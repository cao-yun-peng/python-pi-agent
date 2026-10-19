# 第 5 章：Durable Event Log 与 Replay

这一章解决的问题是：存储中的哪些数据才是会话事实，以及进程中的消息列表丢失后怎样确定性地恢复它。

## 1. 从保存结果到保存事实

上一章已经可以保存 `SessionSnapshot.messages`。但如果只把最新消息列表当作事实来源，我们只能看到“现在是什么”，看不到它按什么顺序形成。

本章改为保存追加式事实：

```text
DurableSessionEvent(sequence=1, message=user(...))
DurableSessionEvent(sequence=2, message=assistant(...))
                         │
                         └── replay_session(...) -> SessionSnapshot(version=2)
```

当前只有 `message_committed` 一种事件。这是刻意保持的小切片：先学会事件序号、校验和投影，再增加分叉、压缩或迁移事件。

## 2. Definition、Provider 与 Consumer

- Definition：`DurableSessionEvent` 定义会话 ID、连续序号、事件种类和已提交消息。
- Transition：`prepare_session_append()` 把旧事件和新消息计算为 `SessionAppendPlan`，`replay_session()` 把事件折叠为 snapshot。
- Provider：`InMemorySessionStore` 只把 plan 中的新事件按 session 保存。
- Consumer：`AgentSession` 仍只依赖 `SessionStore.load()` 与 `append()`，无需知道 replay 细节。
- Evidence：`test_session_log.py` 验证正常回放、序号断裂、串错 session 和过期写入。

这保持了前面建立的依赖方向：runtime 消费端口，具体日志 Provider 位于 runtime 外部。

## 3. Replay 是状态折叠

`replay_session()` 从空状态开始，按序读取事件：

```python
messages = []
expected_sequence = 1

for event in events:
    verify_session_and_sequence(event, expected_sequence)
    messages.append(event.message)
    expected_sequence += 1
```

它不访问网络、磁盘、时钟或全局变量，因此相同事件一定产生相同快照。`prepare_session_append()` 遵循相同原则：它返回不可变计划，不修改输入事件。纯 transition 让内存、JSONL、未来的 SQLite 或远程存储共享同一套语义。

## 4. 为什么严格检查序号

事件 `[1, 3]` 不能被当作正常历史，因为缺失的 `2` 可能正是一个 tool result、权限决定或 assistant message。静默跳过会让模型看到不真实的上下文。

同理，`session_id="B"` 的事件不能混入 A。当前实现遇到两种情况都会抛出 `SessionReplayError`，要求存储层或操作者先修复日志。

## 5. Version 的新语义

`SessionSnapshot.version` 现在表示“已经成功 replay 的最后事件序号”。当前一个事件对应一条消息，所以 version 恰好也等于消息数；这是当前事件模型的结果，不是 version 的定义。

未来加入不直接产生消息的事件后，两者可以不同：

```text
event count = 5
message count = 4
snapshot version = 5
```

乐观并发仍使用 version：只有调用者基于日志尾部版本写入，append 才能成功。

## 6. 运行示例

在仓库根目录执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe .\python-pi-agent\examples\05_session_replay.py
```

示例先 append 两条消息，再丢弃 append 返回的快照，只保留事件并调用 `replay_session()`。这证明快照可以由日志重新计算。

## 7. Durable 不等于已经落盘

这里的 durable 指“重要状态被表达为可持久化、可回放的有序事实”。`InMemorySessionStore` 在进程退出后仍会丢失数据；下一章的 JSONL Provider 会增加跨进程恢复，同时保留相同事件与 replay 语义。

## 8. 与 Pi / DeepSeek Harness 的映射

- 对应 Pi session entries 的思想：会话由按序条目恢复，而不是依赖某个活着的 Agent 对象。
- 对应 DeepSeek Harness 的 capability seam：事件是 Definition，内存存储是 Provider，`AgentSession` 是 Consumer，测试和可打印日志是 Evidence。
- 当前主动省略分支树、快照缓存、压缩事件和持久化格式，防止第一版 replay 被生产级复杂度淹没。

## 9. 掌握标准

完成本章后，应能独立解释：

1. 为什么 event log 是事实来源，而 snapshot 是投影。
2. 为什么 replay 应尽量保持为纯函数。
3. 为什么 sequence gap 不能静默忽略。
4. 为什么 version 是日志位置，不应定义成消息数量。
5. 为什么内存事件日志仍不等于跨进程持久化。
