# 第 19 章：Session-aware 插件与生命周期租约

## 要解决的问题

普通插件只在进程级注册工具和 Hook，无法安全地保存某个会话自己的状态。若插件自行维护全局 `dict[session_id, state]`，很容易出现三个问题：

1. 两个 Session 误用同一份可变状态；
2. Session 结束后资源没有释放；
3. 插件直接改写消息历史，导致内存请求与 durable replay 不一致。

这一章把插件生命周期切成两层：`PluginHost` 管理进程级注册，`PluginSessionLease` 管理一个 Session 的实例、请求拦截器、观察任务和释放顺序。

## 最小契约

插件通过显式 factory 注册，而不是 import 时自动注册：

```python
class SessionPlugin(Protocol):
    async def before_provider_request(
        self,
        context: ProviderRequestContext,
        plan: ContextPlan,
    ) -> ContextPlan: ...

    async def on_run_settled(self, run_id: str) -> None: ...

    async def close(self) -> None: ...
```

`SessionPluginContext` 当前只暴露不可变的 `session_id`。它没有 `SessionStore`、canonical messages 或内部队列，因此插件不能绕过 durable transition。

## 请求投影而不是历史改写

`before_provider_request()` 接收 `ProviderRequestContext + ContextPlan`，可以给这一次 Provider 请求增加系统消息：

```python
async def before_provider_request(self, context, plan):
    return ContextPlan(
        messages=(
            Message(role="system", content=f"session:{context.session_id}"),
            *plan.messages,
        ),
        source_message_count=plan.source_message_count,
        omitted_message_count=plan.omitted_message_count,
        token_budget=plan.token_budget,
        estimated_tokens=plan.estimated_tokens,
    )
```

运行时验证返回类型和 `source_message_count`。插件能变换模型可见投影，但 durable history 仍只保存真实 user/assistant/tool 消息。重启后 replay 不依赖插件是否还安装。

## 打开、执行与关闭

执行顺序如下：

```text
PluginHost.load(factory)
  -> 只发布 SessionPluginFactory

AgentSession.restore()
  -> PluginHost.open_session(session_id)
  -> 每个 factory 创建独立实例
  -> 固定为 PluginSessionLease

每次 Provider 请求
  -> ContextStrategy 生成 ContextPlan
  -> lease 按 priority/注册顺序调用 interceptor
  -> Provider 收到最终投影

run 结束
  -> 后台通知 on_run_settled

AgentSession.close()
  -> abort/等待 idle
  -> 等待观察任务
  -> 逆序 close SessionPlugin
  -> 从 host 释放 lease
```

使用异步上下文管理器可以避免忘记关闭：

```python
async with AgentSession(
    "demo",
    store,
    AgentComponents(provider),
    plugin_host=host,
) as session:
    await session.run("hello")
```

## 失败、超时与取消

- factory 创建失败：已创建的实例逆序关闭，Session restore 失败，不发布半成品 lease；
- request interceptor 超时或抛错：包装成 `PluginExecutionError`，Provider 不会被调用；
- request interceptor 被取消：原样传播 `CancelledError`，后续插件不执行；
- settled observer 抛错：记录 capability event，不改变 run 结果；
- 活动 lease 存在时卸载插件：拒绝卸载，防止同一 Session 中途更换 Hook 集合；
- host 关闭：先关闭所有 Session lease，再反向卸载进程级插件。

这些语义把“会影响执行结果的拦截器”和“只负责观察的回调”明确分开。

## 可观察轨迹

运行：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\19_session_aware_plugin.py
```

可看到 Provider 请求包含插件注入的 system message，而 Store 中只有 `hello` 与 `done`。同时输出 request ID、settled run ID 和 close，证明实例与 Session 生命周期绑定。

确定性测试在 `tests/test_plugins.py`，覆盖双 Session 隔离、投影不污染 replay、超时、abort、活动 lease 卸载保护和 host close。

## 与 Pi 的对应关系

Pi 的 extension runner 绑定 AgentSession，扩展事件也能读取会话上下文。本实现保留“扩展按会话运行”的思想，但主动收窄能力：

- 不提供 UI、命令、MCP 或目录自动发现；
- 不允许插件直接改 canonical history；
- 不支持运行中的热重载；
- 尚未给插件提供 steer/follow-up 控制端口。

未来若增加控制端口，必须复用 `control_enqueued -> control_claimed -> control_delivered/control_cancelled`，不能把内存 deque 暴露给插件。
