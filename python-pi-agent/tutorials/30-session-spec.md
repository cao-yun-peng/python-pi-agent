# 第 30 章：按 Session 解析组件与 metadata

应用可能为研究助手和客服助手选择不同工具、系统提示词或模型。Session controller 在池中复用，因此每次打开都要先解析和核对规格。这个步骤位于应用接收请求与恢复 durable history 之间。

在安装项目后，从 `python-pi-agent` 运行 `python examples/31_session_spec.py`。示例导入正式包，使用 FakeProvider 完成工具调用。输出显示两种 profile 对应的 metadata、完成结果和一次冲突拒绝。对应测试位于 `tests/test_session_spec.py`。

## 一次打开的路径

`SessionOpenRequest` 提供可选的 `user_id`、`tenant_id`、`profile` 和 JSON `metadata`。调用方把它传给 `runtime.open_session(session_id, request=request)`，也可以传给便捷接口 `runtime.run(session_id, prompt, request=request)`。

`SessionSpecResolver.resolve(session_id, request)` 返回包含完整 `AgentComponents` 和观察 metadata 的 `SessionSpec`。Runtime 每次打开都会调用 resolver，包括已有 controller 的情况。解析在池锁外执行，允许其他 Session 同时打开。解析成功后，在锁内核对已有规格，再复用或创建 controller。解析失败会保留原始异常链并抛出稳定错误，解析取消会传播到调用方。Runtime 关闭时先取消并等待正在执行的解析，再释放 owned resolver。

默认的 `StaticSessionSpecResolver` 返回应用组装后的组件和空 metadata。应用可以调用 `with_session_spec_resolver(resolver, ownership="owned")` 注册自己的 resolver。已有实例默认由调用方管理。调用 `with_session_spec_resolver_factory(RoleResolver)` 时，Builder 会把已完成 Profile、Provider 组合层及插件组装的默认组件传给 factory，并管理它返回的 resolver。factory 在构造 Runtime 前执行，返回的实例在 Runtime 关闭时释放。

## 什么算相同规格

规格在一个 controller 生命周期内固定。工具及其他能力端口按实例身份比较，系统提示词和请求上限按值比较。新建 `AgentComponents.replace(...)` 可以复用相同的能力实例。每次解析都新建 Provider、Tool 或策略会产生不同规格，因此 resolver 应缓存和复用已组装组件。

metadata 也参与规格核对。租户或角色变化而继续打开同一个 ID，会得到 `RuntimeStateError`。冲突检查发生在 TTL 驱逐之前，已到期但仍留在池中的 controller 也遵循这条规则。controller 实际驱逐后，再打开会重新解析和恢复 durable history。应用需要为同一历史保持确定的组件选择，并校验调用方的访问权限。

resolver 输出是完整规格，应用可从默认组件派生后调整工具和 prompt。直接注册独立 resolver 时，它负责给出完整组件。返回组件内的 Provider、工具和策略由应用或 resolver 管理，Runtime 只管理 controller 和明确注册的资源。替换默认 Provider 时，已有默认模型的价格无法用于新的模型，此时用量仍统计，成本标为缺价。

## metadata 的传播与快照

`SessionSpec.metadata` 和 `RuntimeSession.metadata` 保存独立 JSON 快照。键必须是字符串，值必须是有限 JSON 值。嵌套列表和字典在读取时返回副本，调用方修改原输入或读取结果时，controller 的规格保持固定。应用自行约束字段、长度和业务 schema。

Session 的观察事件在 `event.envelope.metadata` 中携带这份快照。Session controller 使用绑定的 sink，异步模型组合层和插件任务通过 Runtime 自己持有的 ContextVar 接收相同观察上下文。退出作用域时恢复原上下文，因此并发 Session 可以共享组件。直接取消 RunHandle 时，绑定的 sink 仍会补齐目标 controller 的 metadata。

`agent_event_record()` 和 `serialize_agent_event()` 输出 metadata。`RedactingEventSink` 对其中的嵌套敏感键执行既有脱敏规则。遥测投影会清空 metadata，结构化日志与 OTel 指标只接受固定字段。模型上下文由组件和 SessionStore 中的消息构造，观察 metadata 沿上述观察通道流转。

请求中的 hints 由 resolver 验证和选择后写入规格。示例只把已选择的 tenant/profile 放入 metadata。授权仍由应用定义，`user_id` 或 `tenant_id` 字符串本身不能证明身份。SessionSpec 和观察 metadata 存在于 Runtime 内存中，重启时由 resolver 重建。

## 练习与设计对照

在示例中为第三个 profile 复用既有工具实例，验证同一 ID 的相同 profile 可以重新打开。随后改变 metadata 中的 tenant，观察冲突异常。再用测试里的阻塞 resolver 取消一次打开，确认池容量仍可用于后续请求。

Pi 的组装参考固定在 `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`，具体文件与符号见[上游映射](../docs/upstream-mapping.md)。本节采用显式 resolver 和 controller 固定规格，后续身份授权、持久化配置版本和多租户 namespace 需要独立的业务契约。
