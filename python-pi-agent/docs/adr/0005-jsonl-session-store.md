# ADR 0005: Versioned Single-Writer JSONL Session Store

## Status

Accepted

## Context

ADR 0004 已让 durable events 成为会话事实来源，但第一份 Provider 仅保存在内存中，进程退出后无法恢复。需要一个依赖标准库、可直接检查且复用现有 replay 契约的持久化切片，同时不能让文件格式和 I/O 进入 Agent loop。

## Decision

- 新增实现现有 `SessionStore` 端口的 `JsonlSessionStore`。
- 一个文件可保存多个 session；每行保存一个带 `schema_version` 的完整 `DurableSessionEvent`。
- 读取时严格校验字段集合、字段类型、格式版本、事件 kind 和领域对象约束。
- JSON/结构损坏映射为 `SessionReplayError`，文件 I/O 失败映射为 `SessionStorageError` 并保留异常链。
- append 复用现有输入校验与 replay，并遵守和内存 Provider 相同的 version 检查与事件生成语义。
- 阻塞文件 I/O 使用 `asyncio.to_thread()`；同一 Store 实例用 `asyncio.Lock` 串行读写。
- 每批追加完成后调用 `flush()` 和 `os.fsync()`。
- 当前明确采用单写者模型，不声称支持多个进程并发写入同一文件。

## Alternatives

- 覆盖保存完整 snapshot：读取简单，但每次写入都重写全部历史，也丢失事件事实。
- 立即使用 SQLite：能提供事务和多连接并发，但会让第一份磁盘 Provider 同时引入 SQL、schema 和事务教学负担。
- 使用 pickle：领域对象还原方便，但格式不透明，存在不可信输入代码执行风险，也不适合长期迁移。
- 不执行 fsync：速度更快，但 append 返回后数据可能仍只在用户态缓冲区中。

## Consequences

- 全新 Store 和 AgentSession 可以在进程重启后恢复完整消息、工具调用和结构化工具结果。
- 文件可以人工审查，损坏记录会显式阻止 replay。
- 每次 load/append 都扫描整个文件，成本为 O(全部事件)，仅适合教学和小型单写者应用。
- 两个独立进程仍可能同时读到相同 version 并交错追加；需要文件锁或事务数据库才能解决。
- 突然断电仍可能留下不完整尾行，下一次加载会报告 replay 错误而不是静默忽略。

## Verification

`tests/test_jsonl_session_store.py` 覆盖完整消息 round-trip、新 Store 实例恢复 AgentSession、过期 version、损坏日志、I/O 错误和取消后的用户消息保留。`examples/06_jsonl_persistence.py` 支持在两个独立 Python 进程中执行 seed 与 resume。
