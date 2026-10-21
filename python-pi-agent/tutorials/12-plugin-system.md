# 第十二章：显式、可释放的插件系统

## 1. 插件不是“自动 import 一堆 Python 文件”

插件真正解决的是一组能力的**所有权和生命周期**：谁注册工具，谁决定 Hook 顺序，初始化失败时哪些内容可见，结束时谁释放连接、任务和监听器。

本章的最小链路是：

```text
PluginFactory
  -> PluginAPI 暂存 Tool / NextTurnHook / cleanup
  -> 冲突校验
  -> 原子 commit
  -> PluginHost.snapshot()
  -> Agent 显式消费 capabilities
  -> PluginHost.close() 逆序释放
```

## 2. 三个主要对象

`PluginFactory` 是插件入口。它接收受限的 `PluginAPI`，而不是拿到整个 runtime：

```python
def my_plugin(api: PluginAPI) -> None:
    api.register_tool(my_tool)
    api.register_next_turn_hook(my_hook, priority=10, timeout_seconds=2)
    api.register_cleanup(close_resource)
```

`PluginHost` 是能力和资源的所有者。`load()` 成功前，其他代码看不到任何注册；`unload()` 先撤掉能力，再反向执行 cleanup。

`PluginCapabilities` 是组装快照：

```python
host = PluginHost()
await host.load("my-plugin", my_plugin)
capabilities = host.snapshot()

agent = Agent(
    provider,
    tools=capabilities.tools,
    next_turn_hook=capabilities.next_turn_hook,
)
```

这一步看似多写了几行，却让依赖非常清楚：插件没有通过 import 偷偷修改全局状态，Agent 也不知道 PluginHost 的存在。

## 3. 为什么需要 staging 和原子 commit

假设 factory 依次注册工具、创建连接、注册 Hook，最后初始化失败。如果前两项已经对 runtime 可见，就产生“加载失败但半个插件仍在运行”的状态。

因此 `PluginHost.load()` 先把注册放进 `_StagedPluginAPI`。只有 factory 返回且工具冲突检查通过后，才整体加入 host。失败时不会发布能力，并逆序调用已经登记的 cleanup。这对应数据库事务里的“全部成功或全部不发生”。

插件创建资源后应立即登记 cleanup，而不是等到 setup 最后一行：

```python
resource = await open_resource()
api.register_cleanup(resource.close)
await initialize_rest(resource)
```

这样后续初始化失败时，host 仍知道如何回滚资源。

## 4. Hook 顺序与数据流

多个 `NextTurnHook` 按 `(priority, registration_order)` 执行，较小 priority 先运行。同优先级保留注册顺序。每个 Hook 的输出是下一个 Hook 的输入：

```text
messages
  -> hook A(priority=-10)
  -> hook B(priority=0, first registered)
  -> hook C(priority=0, second registered)
  -> Agent 下一次模型请求
```

这类 Hook 是拦截器，不是观察者：Agent 必须等待它，它可以改变后续输入，也可以阻止运行。因此超时和异常不能只记日志后继续；当前实现会发出 `plugin_hook_failed`，再抛出 `PluginExecutionError`。外部任务取消则发出 `plugin_hook_cancelled` 并继续传播 `CancelledError`。

`EventSink` 仍是观察通道。它不参与上面的返回值链，也不应承担阻塞 I/O。

## 5. 为什么暂时不把插件 Hook 接入 AgentSession

`AgentSession` 的消息事实来源是 durable log。任意插件若直接替换 `NextTurnHook` 返回的历史，却不写 durable event，就会出现：模型看见一份历史，进程重启 replay 出另一份历史。

因此第一版只把插件能力快照显式交给普通 `Agent`。未来把会话 Hook 接进 `AgentSession` 时，必须二选一：

1. Hook 只修改单次请求的 `ContextPlan`，不宣称改变 canonical history；
2. Hook 返回可验证、可持久化的 session transition，再由 Store 原子提交。

这个限制不是功能缺失，而是在保护已经建立的 durable replay 不变量。

## 6. 与 Pi 的对应关系

| 本项目 | Pi | 本项目的主动取舍 |
|---|---|---|
| `PluginFactory` | `ExtensionFactory` | 都使用受限 API 初始化 |
| `PluginAPI` | `ExtensionAPI` | 目前只开放 Tool、NextTurnHook、cleanup |
| staging + commit/rollback | loader 的 commit/discard | 保留原子发布思想 |
| `PluginHost` | loader + ExtensionRunner 的一部分 | 不包含 UI、命令和模型注册 |
| 有序 Hook chain | ExtensionRunner handler dispatch | 显式 priority；拦截异常 fail-closed |
| `Registration.unregister()` | `pi.on()` 返回的 unsubscribe | 所有注册统一拥有撤销句柄 |
| `host.close()` | `session_shutdown` / runtime teardown | 逆序 cleanup，无自动热重载 |

对照的 Pi commit 是 `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`，关键参考为 `core/extensions/types.ts`、`loader.ts` 和 `runner.ts`。

## 7. 动手练习

1. 运行 `examples/12_plugin_system.py`，先预测 Hook 看到的 role 顺序。
2. 注册两个同名工具，确认第二个插件整体加载失败且 cleanup 执行。
3. 调换两个 Hook 的 priority，观察调用顺序而不修改 Agent Loop。
4. 写一个阻塞 Hook 并把 timeout 设为 `0`，检查 `plugin_hook_failed`。
5. 保存 `register_tool()` 返回的 `Registration`，调用两次 `unregister()`，证明它是幂等的。

## 8. 掌握标准

- 能解释 factory、API、host、capability snapshot 各自负责什么。
- 能说明原子发布为何避免半加载插件。
- 能区分观察者和拦截器的错误策略。
- 能解释 priority、注册顺序、超时和取消如何共同决定 Hook 行为。
- 能说明为什么 session-aware 插件变换必须进入 durable log 或只做请求投影。
