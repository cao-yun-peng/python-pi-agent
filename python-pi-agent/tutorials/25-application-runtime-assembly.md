# 第 25 章：AgentRuntime、Profile 与统一生命周期

## 1. 为什么不能继续堆构造参数

`Agent` 应只解释模型流和工具循环，`AgentSession` 应只解释单会话的 durable history、控制队列和恢复。如果 CLI、Web、测试都直接创建 Provider、Store、PluginHost 和事件 worker，每个入口都会形成一套略有不同的关闭顺序。

本章增加应用层 facade，而不复制 Agent loop：

```text
Loaded config + AgentProfile + explicit catalogs
                    |
                    v
           AgentRuntimeBuilder
                    |
                    v
              AgentRuntime
                    |
             RuntimeSession lease
                    |
                AgentSession
```

## 2. 最小离线示例

从仓库根目录运行：

```powershell
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
python .\python-pi-agent\examples\24_application_runtime.py
```

示例使用 `FakeProvider` 连续运行同一 session。第二次请求能看到第一次的 durable history，退出 `async with runtime` 后会统一关闭 owned resources。

## 3. Builder 与 Profile 的区别

`AgentProfile` 是代码拥有的产品默认值：system prompt、工具 factory、策略 factory 和请求上限。它不保存 API key，也不打开 HTTP client。

`AgentRuntimeBuilder` 消费 Profile、配置和显式依赖，完成一次事务化组装。Builder 只能 build 一次；任何 factory/plugin 初始化失败都会关闭已经创建的 owned resources。

配置驱动组装仍要求显式 Catalog：

```python
runtime = await (
    AgentRuntimeBuilder.from_loaded_config(
        loaded,
        provider_catalog=default_provider_catalog(),
    )
    .with_profile(AgentProfile(name="assistant", system_prompt="..."))
    .build()
)
```

调用 `default_provider_catalog()` 是显式动作，不会在 import 阶段注册全局 Provider。

## 4. owned 与 borrowed

调用方传入的对象默认是 borrowed，Runtime 不关闭它。Builder 自己从 Catalog 创建的 Provider 是 owned，会在 Runtime 关闭时释放内部 HTTP client。

```python
builder.with_provider(external_provider)  # borrowed
builder.with_provider(provider, ownership="owned")
```

关闭顺序固定为 Session → PluginHost/其他组件 → Provider/Store → observer。Observer 最后关闭，因此 plugin/provider cleanup 期间的事件仍然可以送出。

## 5. Session lease 与容量

`open_session()` 返回 `RuntimeSession` lease。同一个 ID 共享一个活动 controller，但每个调用者必须释放自己的 lease：

```python
async with await runtime.open_session("demo") as session:
    await session.run("hello")
```

达到容量时，Runtime 只能驱逐无 lease 且 idle 的 controller；durable history 仍在 `SessionStore`，以后重新打开会恢复。TTL 驱逐是惰性的，只在下一次 open 时检查，因此不会新增无法关闭的后台 worker。

## 6. 失败和取消

- Builder 失败：逆序 rollback，再抛出不包含密钥或原始异常正文的 `RuntimeBuildError`；
- 容量耗尽：抛出 `RuntimeCapacityError`，不会偷偷取消活动 Session；
- Runtime closing/closed：拒绝新 run，抛出 `RuntimeStateError`；
- 关闭失败：继续尝试其他 cleanup，最终以 `RuntimeCloseError.failures` 提供类型化诊断；
- grace period 到期：关闭 Session 并把取消传播到 Provider/Tool。

## 7. 当前主动省略

本切片没有加入 EventHub、RunHandle、多租户 `SessionRef`、正式 CLI 或 Web。下一步先用正式 CLI 验证 facade；Web/SSE 必须在 session namespace、订阅背压和断线语义明确后接入，不能让 HTTP handler 直接持有 `AgentSession`。

## 8. 掌握标准

- 能解释 Builder、Profile、Plugin 与 Runtime 各自负责什么；
- 能判断一个资源应为 owned 还是 borrowed；
- 能预测正常关闭与构造失败回滚的顺序；
- 能说明为什么 RuntimeSession 是 lease，而不是新的 Agent loop；
- 能用 FakeProvider/FakeCloser 证明关闭、取消和 Session 复用语义。
