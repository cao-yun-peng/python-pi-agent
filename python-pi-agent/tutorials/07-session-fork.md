# 第 7 章：持久化会话分叉

这一章解决的问题是：怎样从一个会话的某个历史版本创建新会话，并让两个会话在那之后独立演进。

## 1. Fork 不是修改源日志

假设源会话有三个 durable event：

```text
source: 1 -> 2 -> 3
```

从 version 2 分叉后，存储中有两条独立日志：

```text
source: 1 -> 2 -> 3
branch: 1 -> 2
```

随后向 branch 追加消息不会删除或修改 source。Fork 的含义是复制历史前缀并创建新的 session identity，不是在原日志中移动游标。

## 2. 三层职责

`SessionStore.fork()` 把三层连接起来：

```text
SessionStore 端口
    ↓ 规定调用契约
InMemorySessionStore / JsonlSessionStore
    ↓ 读取、冲突检查、持久化
fork_session_events()
    ↓ 纯计算与 replay 校验
新的 DurableSessionEvent 元组
```

- `fork_session_events()` 不做 I/O，只验证并复制事件。
- Provider 判断目标会话是否已经存在，并负责原子保存。
- 调用方只依赖 `SessionStore`，无需知道事件最终在字典还是 JSONL 文件中。

## 3. 调用方式

```python
branch = await store.fork(
    source_session_id="source",
    target_session_id="branch",
    at_version=2,
)
```

`at_version=None` 表示复制完整源历史。返回值是目标会话的 `SessionSnapshot`，因此可以直接使用它的 version 继续追加：

```python
await store.append("branch", branch.version, [new_message])
```

## 4. 为什么必须拒绝已有目标

如果 `branch` 已经存在，fork 会抛出 `SessionConflictError`，而不是覆盖它。覆盖会让一次拼写错误变成不可恢复的数据丢失，也会破坏 append-only 日志的含义。

冲突检查和写入必须在同一个临界区：内存 Provider 的 fork 方法中没有 `await`，一次调用不会在中途让出控制；JSONL Provider 则在同一把 `asyncio.Lock` 中完成读取、检查和追加。

当前锁只协调同一个 `JsonlSessionStore` 实例。两个进程同时写同一文件仍需要文件锁或事务数据库。

## 5. 为什么持久化层拒绝空分支

纯函数允许 `at_version=0`，因为“复制空前缀”在状态转换上是成立的。但当前 durable log 只有 `message_committed` 事件：如果目标分支没有任何事件，JSONL 文件里就没有证据证明它存在。

为了让内存和 JSONL Provider 保持一致，`SessionStore.fork()` 暂时要求至少复制一个事件。未来加入 `session_created` 元数据事件后，才能可靠持久化空分支。

## 6. 运行示例

在仓库根目录执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe .\python-pi-agent\examples\07_session_fork.py
```

输出中 source 保留第三条消息，branch 从前两条消息开始追加自己的第三条消息。示例重新创建 `JsonlSessionStore` 后再读取两条分支，用来证明 fork 已经真正写入磁盘。

## 7. 与 Pi 的映射和主动省略

Pi 的 runtime fork 还负责切换活跃 session、销毁旧运行时、重新绑定扩展和恢复 UI；本章只实现其中的持久化分支能力。运行时会话切换属于下一层 Consumer，不应塞进 `SessionStore`。

当前主动省略：父子分支元数据、分支列表、entry ID、`before`/`at` 位置语义、跨进程事务和 session 生命周期事件。

## 8. 掌握标准

完成本章后，应能独立解释：

1. transition 为什么不能直接写文件。
2. Provider 为什么必须拒绝覆盖已有目标会话。
3. fork 返回 snapshot 后怎样继续使用乐观版本追加。
4. 为什么空事件元组无法证明一个 JSONL session 已经存在。
5. 为什么存储分叉和运行时切换是两项不同职责。
