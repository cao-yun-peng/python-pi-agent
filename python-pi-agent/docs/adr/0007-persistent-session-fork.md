# ADR 0007: Persistent Session Fork

## Status

Accepted

## Context

durable event log 已经支持 append、replay 和跨进程恢复，但调用方还不能从某个历史 version 创建独立会话。Fork 必须在不修改源日志的前提下复制历史前缀、拒绝覆盖已有目标，并让内存与 JSONL Provider 具有一致语义。

## Decision

- `SessionStore` 新增异步 `fork(source_session_id, target_session_id, at_version=None)` 端口方法，返回目标 `SessionSnapshot`。
- `fork_session_events()` 是共享纯 transition：完整 replay 源日志、验证 version、深拷贝消息中的嵌套可变 JSON，并生成目标事件流。
- Provider 负责检查目标 session 不存在并保存 transition 输出；冲突抛出 `SessionConflictError`。
- `InMemorySessionStore` 在不让出事件循环控制的临界区内完成检查与赋值。
- `JsonlSessionStore` 在同一实例锁内完成读取、冲突检查和追加，并沿用 flush/fsync 写入路径。
- 当前没有 `session_created` durable event，空事件流无法跨进程表示目标 session 已存在，因此持久化 fork 暂不允许 `at_version=0` 或空源日志。

## Alternatives

- 让每个 Provider 自己实现复制规则：会造成 version、深拷贝和 replay 校验语义漂移。
- 覆盖已有目标：会破坏 append-only 历史，并让名称冲突导致数据丢失。
- 只保存 parent/version 引用而不复制事件：读取目标时必须跨日志解析祖先链，超出当前教学切片。

## Consequences

- 源和目标拥有独立事件流，可以在共同前缀后分别追加。
- JSONL 文件会重复保存共同前缀，以空间换取简单、独立的 replay。
- 同实例并发 fork 得到保护；跨进程竞争仍可能同时通过目标检查，需要未来的文件锁或事务 Provider。
- 当前日志不能表达空分支、父子关系或分支列表。

## Verification

- transition 测试覆盖历史前缀、完整复制、损坏日志、version 边界和嵌套 JSON 隔离。
- 内存 Provider 测试覆盖分叉后独立追加、目标冲突和空分支拒绝。
- JSONL Provider 测试覆盖重启恢复、分叉后独立追加以及冲突目标不被覆盖。
- `examples/07_session_fork.py` 展示完整的持久化分叉轨迹。
