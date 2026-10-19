# 第 6 章：JSONL 持久化

这一章解决的问题是：如何让 durable event log 不只存在于内存，并在 Python 进程退出后用同一套 replay 逻辑恢复 Agent 上下文。

## 1. 为什么选择 JSONL

JSONL 每一行都是一个独立 JSON 对象，天然匹配追加式事件：

```text
append message
→ DurableSessionEvent
→ JSON object + newline
→ flush + fsync
```

与一次覆盖整个 JSON 数组相比，追加新事件不需要重写全部历史。文本格式也方便学习者直接检查和故意损坏日志，观察恢复失败路径。

## 2. 文件记录格式

每一行包含格式版本和完整事件：

```json
{"schema_version":1,"session_id":"demo","sequence":1,"kind":"message_committed","message":{"role":"user","content":"你好","tool_calls":[],"tool_call_id":null,"tool_name":null,"is_error":false,"structured_content":null}}
```

`schema_version` 描述磁盘格式，不等于 `SessionSnapshot.version`：

- `schema_version`：这一行按哪一版规则编码。
- `sequence`：事件在这个 session 中的位置。
- `SessionSnapshot.version`：replay 完成后的日志位置。

## 3. 严格边界验证

JSONL 是外部输入，不能因为 JSON 语法正确就直接信任。解码器会验证：

- event、message 和 tool call 的字段是否完整且没有未知字段；
- 字符串、整数、布尔值、数组和对象的形状是否正确；
- `schema_version`、事件 kind 和消息 role 是否受支持；
- 解码后的 `Message` 是否满足领域约束。

JSON 语法或记录结构损坏会抛出 `SessionReplayError`；文件读取和写入失败会抛出 `SessionStorageError`。两者分开后，调用方能判断是数据已损坏，还是基础设施暂时不可用。

## 4. Provider 没有改变 runtime

`JsonlSessionStore` 和 `InMemorySessionStore` 实现相同的 `SessionStore` 端口：

```python
session = AgentSession(
    "demo",
    JsonlSessionStore("sessions.jsonl"),
    provider,
)
```

`AgentSession`、`Agent` 和工具循环都不需要知道事件来自内存还是文件。这就是端口与 Provider 分离带来的复用能力。

## 5. 追加过程

一次 append 的顺序是：

```text
读取并严格解码整个日志
→ 筛选当前 session 的事件
→ replay 当前 snapshot
→ 比较 expected_version
→ 生成连续 sequence 的新事件
→ 编码为 JSONL
→ 单次追加写入
→ flush + fsync
→ 返回新 snapshot
```

文件 I/O 通过 `asyncio.to_thread()` 移出事件循环。实例内 `asyncio.Lock` 防止同一个 Store 对象出现交错读写。

## 6. 跨进程运行示例

在同一个 PowerShell 中执行两次 Python：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
$demoLog = Join-Path $env:TEMP "pi-agent-jsonl-$PID.jsonl"

.\.venv\Scripts\python.exe .\python-pi-agent\examples\06_jsonl_persistence.py seed $demoLog
.\.venv\Scripts\python.exe .\python-pi-agent\examples\06_jsonl_persistence.py resume $demoLog
```

`seed` 进程结束后，`resume` 创建全新的 Store 和 AgentSession。第二次模型请求仍包含第一次的 user/assistant 消息，证明上下文来自 JSONL replay，而不是旧 Python 对象。

## 7. 失败与取消

- 遇到半行 JSON、字段缺失或未知 schema version 时，整个读取失败，不会用部分可信历史继续请求模型。
- Provider 被取消时，已经完成 JSONL append 的用户消息仍在文件中；未形成完整消息的流式 assistant 内容不会写入。
- I/O 异常保留原始异常链，并映射为稳定的 `SessionStorageError`。

## 8. 当前主动省略的复杂度

这一版是单写者教学实现，不是完整数据库：

- 实例内锁不能协调两个独立进程。
- “读取 version → 追加”不是跨进程原子事务。
- 批量事件在机器突然断电时仍可能只写入一部分。
- 暂无 schema migration、日志轮转、快照缓存和压缩。

生产场景可以实现带事务的 SQLite/PostgreSQL Provider，而不改变 `AgentSession`。JSONL Provider 的价值是把序列化、刷盘、重启恢复和损坏日志语义展示清楚。

## 9. 与 Pi / DeepSeek Harness 的映射

- 对应 Pi 的 JSONL session entries：进程内状态可从磁盘条目重建。
- 对应 capability seam：`SessionStore` 是 Definition，JSONL 是新 Provider，`AgentSession` 是不变的 Consumer，测试是 Evidence。
- 本项目目前不复制 Pi 的分支树、迁移和完整 session metadata，保持学习切片可验证。

## 10. 掌握标准

完成本章后，应能独立解释：

1. 为什么保存 event，而不是反复覆盖 snapshot。
2. 为什么外部 JSON 必须重新做运行时校验。
3. `schema_version`、事件 `sequence` 和 snapshot `version` 的区别。
4. `flush()`、`fsync()` 和真正数据库事务之间的差距。
5. 为什么替换 SessionStore 不需要修改 Agent loop。
