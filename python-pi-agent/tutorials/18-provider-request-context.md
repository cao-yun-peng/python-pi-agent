# 第 18 章：ProviderRequestContext

## 它解决什么问题

Provider 原先只收到 messages 和 tools。日志、路由器或重试层无法可靠回答：

- 这是普通推理还是压缩摘要？
- 属于哪个 session、run 和 turn？
- 两次物理请求是两个逻辑请求，还是同一请求的 retry？

从 prompt 内容猜这些身份既不可靠，也会把业务上下文与敏感内容混在一起。`ProviderRequestContext` 为每次 Provider 调用提供结构化、不可变且不含 prompt/密钥的控制元数据。

## 字段语义

```python
ProviderRequestContext(
    request_id=...,       # 逻辑请求身份
    attempt_id=...,       # 一次物理尝试身份
    attempt=1,            # 重试序号
    purpose="agent_turn", # 或 compaction
    request_number=1,
    session_id=...,
    run_id=...,
    turn_id=...,
    parent_request_id=...,
    cause_event_id=...,
)
```

当前 runtime 尚未实现 retry/router，所以每个请求的 `attempt=1`。先固定契约的价值是以后增加 fallback、多 Provider 或 retry 时不必再次破坏 `ModelProvider` 接口。

## 生命周期位置

普通模型请求在准备上下文前创建 context，随后同一对象依次用于：

```text
ContextStrategy
  -> ProviderRequestHook
  -> model_request_started event
  -> ModelProvider.stream(..., context=context)
  -> model_request_finished / model_request_failed event
```

Observer event 把 request/attempt/session/run/turn 放在 `AgentEventEnvelope`，不再复制到开放 payload。started 与 terminal event 共享 attempt/span ID，terminal envelope 记录 monotonic duration。

压缩请求使用 `purpose="compaction"`，手动压缩可以没有 run/turn；自动压缩会关联触发它的目标 run。这样 telemetry 和路由策略无需检查 prompt 就能区分两类流量。

## 可运行证据

运行：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\18_provider_request_context.py
```

示例分别打印 compaction strategy 与普通 FakeProvider 收到的 context。二者拥有相同 session ID，但 purpose、request ID 和 run/turn 语义不同。

`FakeProvider.request_contexts` 和 `FakeCompactionStrategy.request_contexts` 让测试无需真实 API 即可断言这些身份。对应测试位于 `tests/test_compaction.py`、`tests/test_session_runtime.py` 和 `tests/test_deepseek_provider.py`。

## 失败与取消

context 是请求输入，不承担重试状态机。Provider 失败、流中断或取消沿既有错误路径传播；同一次实际调用始终保留自己的 `attempt_id`。未来 retry 实现必须：

1. 保留逻辑 `request_id`；
2. 为每次尝试创建新 `attempt_id`；
3. 单调增加 `attempt`；
4. 不把认证 header、token 或 prompt 放入 context。

## 与 Pi 的对应关系

Pi 的模型选择和 extension 事件能从 AgentSession 周边取得运行信息。本项目把 Provider 所需身份收敛为显式值对象，避免 Provider 反向读取 Session 全局状态。当前主动省略了 Provider router、fallback、计费归因和跨 Agent trace；这些能力应消费该 context，而不是继续扩展主循环的隐式状态。
