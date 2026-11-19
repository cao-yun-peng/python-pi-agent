# ADR 0040：工具定义的嵌套隔离与 Agent 绑定

状态：已接受。关联 ADR 0002、0014、0034、0035、0036。

## 背景

frozen dataclass 无法自动冻结 input_schema 中的嵌套列表和字典。Agent 初始化读取定义，调用时再读取原工具 definition，可能让模型声明、参数校验、审批、Gate 和超时使用不同版本。MCP 描述对象已具备隔离，但通用 ToolDefinition 尚未覆盖。

## 决定

- ToolDefinition 在创建时复用领域层的有限 JSON 快照映射，输入与返回的嵌套数据独立；顶层不可赋值。未知 schema 关键字仍可保存，支持检查继续属于 SchemaValidator/适配器，快照不等于完整 schema 校验。
- Agent 构造时读取每项工具的 definition 一次，以私有绑定保存定义快照并委托原工具执行。重新核对捕获后的名称冲突，避免 Components 校验之后的名称变化造成静默覆盖。
- 模型声明、参数校验、审批上下文、Gate 所见 Tool.definition 与执行上下文使用该绑定；超时明确读取 context.definition。Gate 依赖 Tool 协议，不依赖原工具的对象身份或具体类。handler 应使用 context.definition 获取本次调用契约。
- 只复制定义元数据，不复制 handler、连接、锁等资源，不改变资源关闭、异常、timeout 或取消传播。向 Provider 提供的 schema 同样无法通过嵌套修改污染绑定。
- 绑定生命周期为 Agent 实例；改变定义需要构造新 Agent。AgentSession 原有每次 run 构造 Agent 的组装点继续捕获当次契约，不增加会话热重载 API、持久化定义或恢复重试。

## 备选方案

只 deepcopy 一次 schema 仍会暴露内部嵌套值；只冻结领域对象不能解决工具替换整份定义；每次调用重新读取定义会产生版本漂移。复制整个 Tool 会复制或破坏活资源，且不满足插件生命周期边界。选择隔离领域值与绑定 Tool 协议的最小组合。

## 后果

input_schema 仍为 Mapping，读取的嵌套值保持普通 JSON dict/list。序列化使用 dict(definition.input_schema)，修改读出的数据不会更新定义。非 JSON、非有限值在定义边界被拒绝；有意更新 schema 应创建新的 ToolDefinition。自定义 Gate 接收到协议适配器，自定义 handler 的内部状态仍由实现负责；绑定不构成 Python 沙箱。

## 验证

test_tool_definition_snapshots 覆盖原始/读取后嵌套修改、Provider 修改、定义 getter 读取次数、名称/参数/审批/Gate/超时一致性、名称冲突、重新组装、原 handler 身份与取消。相邻工具、通用接缝、MCP 和两类 Provider 离线测试验证 Mapping 兼容。示例 41 输出既有事件、校验失败与成功结果；第三章教程解释取舍。
