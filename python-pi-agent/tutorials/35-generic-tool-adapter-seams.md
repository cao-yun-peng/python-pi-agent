# 第 35 章：在接远端工具之前补齐通用契约

本章对应 MCP 开发方案阶段 0。先让“校验、观察、身份与失败”可替换且可验证，下一阶段才能把本地 Tool 接到远端执行端。

安装可选完整校验器并运行离线示例：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\python-pi-agent[jsonschema]"
.\.venv\Scripts\python.exe .\python-pi-agent\examples\36_generic_tool_seams.py
```

示例使用 FakeProvider 和内存 Session。第一轮产生一个非法 code 和一个合法 code：先出现 tool_validation_failed，只有合法调用进入 tool_approval_requested 与 tool_execution_started。最终打印的审批次数为 1。handler 把显式 ToolExecutionContext 交给 worker task，打印的 run/turn 与事件一致。第二轮零超时工具返回 unknown 提示；关闭时插件 cleanup 执行。

## 校验是端口，不是隐含约定

SchemaValidator.supports(schema) 用于发现/注册时预检能力；validate(schema, arguments) 在每次工具调用的审批前执行，失败抛 ToolValidationError。Profile.schema_validator_factory 解析一次，Builder.with_schema_validator 替换它并跳过原 factory，最终保存在 AgentComponents。SessionSpec 将同一实例视为相同选择，另一个实例视为不同规格。

| 实现 | 用途 | 边界 |
|---|---|---|
| SubsetSchemaValidator | 保留旧工具行为和无依赖教学路径 | validate 仍忽略不支持关键字，supports 对未知断言、不能真正检查的嵌套形状返回 false |
| JsonSchemaValidator | 远端/手写复杂 schema | 显式 Draft 2020-12，anyOf/$defs/$ref/pattern 等；不支持的 dialect、必需 vocabulary、未知关键字拒绝 |

supports=false 并不自动阻止默认工具执行：这样保持已有行为。MCP 发现层将负责在发布工具之前检查 supports。普通应用也应在注册复杂工具时显式预检。不要只加一个 pattern 就假设默认校验器会执行它。

完整实现使用 [jsonschema 的 Validator](https://python-jsonschema.readthedocs.io/en/stable/validate/) 和显式的离线 [Registry](https://python-jsonschema.readthedocs.io/en/stable/referencing/)。外部 HTTP/file 引用没有登记时，即使相关参数未提供也会在预检失败；应用可通过 resources 参数预先登记本地 JSON schema，不存在任意 URL 或文件加载器。format 按该 profile 作为标注，不开启隐式网络或格式检查器。

校验不填默认值、不把字符串转换成数字、不修改调用参数。完整 Draft 的整数语义与历史 Python int 子集有细节区别，切换必须显式。schema/资源做独立 JSON 快照，编译缓存仅在实例内保存至多 cache_size 项，默认 32；没有进程全局注册。

## 错误与取消

参数不符合 schema 时，完整实现只生成类似 `$: JSON Schema assertion failed (pattern)` 的稳定说明，最多 8 项；不复制库错误的 message 或 instance path，因为它们可能包含秘密。非法 schema、未解析引用与缺依赖是 ConfigurationError；自定义校验器的原生错误是 InternalError，保留 cause，不能误报成供应商故障。

校验器同步执行。输入先深拷贝，因此实现者原地修改 schema/arguments 也无法改变审批与执行参数。同步正则或递归校验的 CPU 成本不能靠 asyncio.timeout 抢占；后续远端适配器仍需限制 schema/结果大小和复杂度。

工具超时只说明本地没有及时获得结果。新提示明确结果可能未知，不应自动重试；成功发生的外部副作用不能靠 timeout 撤销。取消继续抛 CancelledError，不提交伪造 tool result；Session 恢复仍按已有机制补齐中断调用。tests/test_generic_tool_seams.py 用零超时和 Event 控制两条路径，没有真实等待。

## 插件借观察端口，工具带显式身份

PluginAPI.event_sink 是只读借用：使用的就是 Host 已隔离的观察通道。插件不关闭它，也不能用它改变执行结果；自己创建的资源通过 register_cleanup 释放。缓慢 I/O 仍由 BufferedEventSink 等应用适配器处理。事件属于 best-effort，不能替代 SessionStore。

ToolExecutionContext 新增可选 run_id/turn_id，审批、Gate、handler、worker 与 B5 ToolResultContext.execution 都看到同一调用身份。字段从 loop 的活动身份显式生成，不依赖 worker 恰好继承某个 ContextVar。ToolCall.id 可以重复，不能独立承担跨 run 关联；直接调用工具时两个字段可都为 None。

掌握标准：能用自己的 SchemaValidator 替换默认实现，证明非法参数没有进入审批；能在 worker 中复原 run/turn，并证明 observer 抛错不影响结果或关闭。对应 Pi 的前置工具校验与 Hook 概念、区别及固定 commit 见 upstream-mapping。当前尚未连接任何 MCP server。
