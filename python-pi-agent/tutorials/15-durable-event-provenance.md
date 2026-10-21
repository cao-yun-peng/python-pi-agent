# 第十五章：Durable Event 的 Run、Turn 与 Provenance

## 1. 要解决的问题

只有 `session_id + sequence` 的日志能恢复消息，却不能解释消息的运行归属和来源。一次用户任务可能有多次模型请求、多个工具结果、自动压缩和中断恢复；后续再加入并行或多 Agent 时，仅看消息顺序不足以追责。

本章使用四层身份：

```text
session_id
  └── run_id                  一次用户任务
       └── turn_id            一次模型请求及其工具结果
            └── event_id      一个不可变 durable fact
```

## 2. 领域对象

```python
context = SessionEventContext(
    run_id="run-7",
    turn_id="run-7:turn:2",
    provenance=EventProvenance(
        source="model",
        actor_id="DeepSeekProvider",
        cause_event_id="demo:4",
    ),
)
```

`source` 表示事实生产者类型；`actor_id` 是低敏感度执行者标识；`cause_event_id` 表示直接原因；`copied_from_event_id` 专门表示 fork 来源。不要把 prompt、token 或完整工具输出塞进 provenance。

## 3. 写入路径

`AgentSession` 为每个 run 创建 ID，`Agent` 根据 `request_number` 创建 turn ID。每次 `_commit()` 都把 `Message` 和 `SessionEventContext` 一起交给 Store：

```text
Agent commit
  -> SessionWriter.commit(message, event_context)
  -> SessionStore.append(..., event_context=...)
  -> prepare_session_append()
  -> DurableSessionEvent
```

用户输入标记为 `user`，assistant 标记为 `model`，工具结果标记为 `tool`，中断补齐标记为 `recovery`，压缩事件标记为 `compaction`。

## 4. JSONL v4 与旧日志

schema v4 新增 `event_id`、`run_id`、`turn_id` 和 `provenance`。v1-v3 仍可读取：读取器使用 `<session_id>:<sequence>` 合成稳定 event ID，并标记 `legacy`。新写入不会继续产生 legacy 事件。

fork 后目标事件得到自己的 event ID，例如 `branch:2`，同时 `copied_from_event_id="source:2"`。这样分支可独立追加，又不会丢失来源。

## 5. 运行示例与失败路径

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\15_durable_event_provenance.py
```

损坏的空 ID、只有 turn 而没有 run、未知 provenance source 或不完整 v4 字段都会在领域边界或严格 JSONL 解码阶段失败。取消后的已提交 user/tool 事件仍保留原 run/turn；没有完成的 assistant 流不会产生 durable event。

## 6. 与 Pi 的映射

Pi 的 session entry、turn 生命周期和分支信息提供相似追踪线索。本项目把最小关联字段直接放进 provider-neutral durable event，暂不实现完整分布式 trace、wall-clock timestamp、跨服务 span 或数据库索引。
