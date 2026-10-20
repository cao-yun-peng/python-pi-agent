# 第 10 章：Durable Compaction

上一章的 `TokenBudgetContextStrategy` 只改变一次模型请求的投影：旧消息还在 `SessionSnapshot`，下一次仍要重新选择。本章实现真正的 compaction：把较早的完整 turn 总结成一条模型可见摘要，并把这次替换作为 durable event 持久化。进程重启后，replay 会得到同一个压缩结果。

本章通过 `await AgentSession.compact()` 显式触发。第 11 章在不改变这里的摘要和持久化机制的前提下，加入 token threshold 自动触发。

## 1. 两种上下文缩减不是一回事

```text
ContextStrategy：完整 durable history -> 本次请求临时投影
Compaction：      durable events -> 追加替换事件 -> 新的可重放投影
```

预算裁剪是无损于存储的临时选择；compaction 是有损的语义总结。它不能只修改内存里的 `messages`，否则重启后摘要会消失，也不能直接删改旧 JSONL 行，否则审计、恢复和分叉都会失去原始事实。

## 2. 一次压缩的完整数据流

```text
AgentSession.compact()
  -> SessionStore.load()
  -> prepare_compaction() 选择旧完整 turn
  -> CompactionStrategy.summarize()
  -> build_summary_message()
  -> SessionStore.compact(expected_version, SessionCompaction)
  -> prepare_session_compaction() 生成 context_compacted event
  -> replay_session() 应用替换
  -> SessionSnapshot
```

`prepare_compaction()` 保留开头的 system messages 和最近 `keep_recent_turns` 个 turn。工具调用、tool result 与同一轮最终回答作为整体移动，不会被切断。

`CompactionStrategy` 只有一个职责：接收已经安全选出的消息并返回摘要文字。`ModelCompactionStrategy` 用独立 `ModelProvider` 请求摘要，不暴露工具；因此应用可以使用不同模型，也可以在测试中替换成 `FakeCompactionStrategy`。

## 3. durable event 保存的是“替换操作”

`context_compacted` 的 payload 是：

```python
SessionCompaction(
    replace_start=1,
    replaced_message_count=4,
    summary_message=Message(
        role="user",
        content="<conversation-summary>\n...\n</conversation-summary>",
    ),
)
```

`replace_start` 必须紧跟所有 leading system messages，替换范围必须位于当前 replay 结果内。摘要使用带明确标记的 user message，是为了让通用 Provider 都能消费，而不新增供应商私有 role。

日志仍保留此前的 `message_committed` events，只追加一个新事件。replay 依次应用事实：先重建旧历史，再用摘要替换指定前缀。于是 `SessionSnapshot.version` 是 durable event 数量，不再等于当前 `messages` 数量。

## 4. 为什么写入必须在摘要完成之后

摘要模型可能报错、超时或被取消。`AgentSession` 先在内存中完成选择和摘要，只有得到合法非空文本后才调用 `SessionStore.compact()`。因此失败或取消不会写入半条摘要事件。

Store 在同一临界区内重新读取事件、检查 `expected_version`、生成 transition 并追加 JSONL。如果摘要期间另一个写者更新了会话，写入会以 `SessionConflictError` 失败，而不是覆盖新消息。调用方可以重新加载后决定是否重试摘要。

观察事件依次为：

```text
compaction_started
compaction_completed
```

失败和取消分别产生 `compaction_failed`、`compaction_cancelled`。这些是 capability events，不是恢复状态所依赖的 durable facts。

## 5. JSONL schema v3 与兼容性

schema v3 给事件增加 `compaction` 字段：消息事件的该字段为 `null`，压缩事件的 `message` 字段为 `null`。严格互斥能尽早发现损坏或歧义日志。解码器仍读取 v1 和 v2 消息事件；新写入统一使用 v3。

`JsonlSessionStore` 关闭并重新创建后会读取所有事件，再交给同一个 `replay_session()`。因此内存 Store 与 JSONL Store 不各自实现一套压缩规则。

## 6. 运行示例

在仓库根目录执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe .\python-pi-agent\examples\10_durable_compaction.py
```

示例使用 `FakeProvider` 生成确定性摘要，写入临时 JSONL，重新创建 Store 后再发起下一轮请求。可以同时看到原始事件仍存在、snapshot 已缩短，以及下一次 Provider 收到摘要而不是旧 turn。

## 7. 与 Pi 的映射和主动简化

固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 中，`packages/coding-agent/src/core/compaction/compaction.ts` 负责选择切点、准备摘要输入与生成 summary；session entry 中的 compaction 信息让后续上下文重建识别压缩边界。

本项目保留相同的关键思想：压缩边界可验证、摘要由独立能力生成、结果进入持久会话事实、重启后可恢复。第一版主动省略 Pi 的 split-turn、文件读写跟踪、自动 token 阈值、增量摘要和多种模型元数据；当前只压缩完整 turn，并把摘要保存为通用 `Message`。

## 8. 代码练习与掌握标准

建议亲手完成以下练习，而不是只读实现：

1. 给 `prepare_compaction()` 增加一个测试，证明两个 tool call 和两个 tool result 仍属于同一 turn。
2. 写一个 `CompactionStrategy`，不调用模型，只把每条 user message 提取成项目符号。
3. 修改示例连续压缩两次，观察第二次如何把旧摘要纳入新摘要输入。
4. 模拟摘要完成后、Store 写入前出现并发 append，解释为何版本冲突比覆盖更安全。
5. 设计摘要质量 eval：列出压缩前必须保留的事实，并验证压缩后模型能否回答。

完成本章后，应能解释并实现：临时投影与 durable compaction 的区别、追加式替换事件、乐观并发、失败原子性、重放恢复，以及摘要策略为何是可替换端口。
