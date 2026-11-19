# 第 33 章：签名就是工具契约

工具位于模型生成 ToolCall 与 runtime 提交 tool message 之间。手写 schema 与 handler 可能各自演进；本章让函数签名同时描述两者。

运行正式包示例：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\34_tool_decorator.py
```

示例打印推导的 schema，并展示成功读取、ToolError 与参数类型错误。轨迹依次经过 tool_call_received、校验、tool_execution_started/finished 和 message_committed。类型错误不会调用函数；工具结果进入第二次模型请求。

`@tool` 和 `@tool(...)` 都返回 Tool 值，无 import 注册副作用。把值显式交给 AgentComponents.tools、Builder.with_tools 或 PluginAPI.register_tool。名称默认取函数名，描述取 docstring 第一段，Annotated 的字符串描述参数。str/int/float/bool、同类型 Literal、list[T]、开放 JSON 字典、Optional 与 Annotated 可以组合。必填顺序沿用签名；有默认值的参数交给 Python 默认绑定。ToolExecutionContext 参数隐藏于 schema，执行时注入。整数传给 float 参数时转为浮点数。

Optional 使用 type 列表，所以校验器必须同时支持列表与嵌套 items/properties。不能生成 minimum、pattern 等当前校验器不执行的约束。复杂类型继续用 FunctionTool 和手写 schema；B6 的可插拔 SchemaValidator 留待后续。

返回字符串直接成为 content，None 成为空字符串，JSON 值同时生成排序 JSON 文本与 structured_content，ToolOutput 原样保留。ToolError 的文本是作者主动提供给模型的业务结果；普通异常文本可能含密钥或内部路径，只由 runtime 提供异常类型。取消向上传播，零超时产生现有的 timeout tool result。同步函数被拒绝，因为放入线程后无法取消底层工作。

需要数据库或检索依赖时，用工厂闭包捕获依赖，在闭包内定义带注解的 async 工具；工厂返回工具 tuple，交给 AgentProfile.tool_factories。资源由应用管理，不把 self、连接对象或密钥加入模型参数。

掌握标准：能从打印的 schema 解释必填、默认、Optional；能证明错误参数不执行函数，并区分 ToolError、内部错误和取消。对应 Pi 的显式 ToolDefinition/AgentTool 契约，Python 签名推导装饰器无直接对应；具体 commit 与符号见 upstream-mapping。
