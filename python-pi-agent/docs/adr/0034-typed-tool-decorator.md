# ADR 0034：类型化工具装饰器

状态：已接受。关联：路线图 B6、ADR 0002、0014。

## 背景

FunctionTool 要求分别维护函数、手写 schema 与返回包装，三者容易漂移。

## 决定

`tools/decorator.py` 用 async 函数签名推导 schema，产生显式交给 AgentComponents 或 PluginAPI 的 DecoratedTool，不注册全局对象。保留 fn 和 __wrapped__，执行仍经过既有校验、审批、Gate、超时与取消管线。

只接受 str/int/float/bool、同类型 Literal、list、开放 JSON object、Optional 和 Annotated 描述；ToolExecutionContext 注入而不暴露给模型。拒绝同步函数、可变参数、位置专用参数和复杂对象。默认值必须是有限 JSON 值，保存独立副本。返回 JSON 值映射为 ToolOutput，None 产生空文本。ToolError 是作者明确允许暴露的模型错误，普通异常继续由 loop 隐藏文本。

schema 只使用现有校验器执行的关键字。校验器增加 type 列表，并对 Optional 的非空 object/array 继续递归校验。description/default 仅为标注。

默认值绑定补充：定义阶段使用已有子集校验函数检查默认值与生成的参数 schema 一致，失败转为含工具名/参数名的 TypeError 并保留 cause，不回显默认值。adapter 保存独立于原函数和 schema 标注的默认值深拷贝；执行时只给缺省参数逐调用复制默认值，再使用已有转换规则。显式值（包括 null/false/0/空容器）优先。原始 ToolCall 和审批上下文不补值，`.fn` / `__wrapped__` 仍是原函数；MCP 不填 schema 默认值。

## 备选方案

默认引入 pydantic 增加依赖；完整 JSON Schema 会产生无法执行的约束；线程执行同步函数无法保证取消；装饰即全局注册违反显式组装原则。均不采用。FunctionTool 保留用于复杂手写契约。

只依赖 Python 的默认绑定会跳过类型校验并共享可变对象；从公开 schema.default 在每次调用重新取值会被外部修改。选择私有快照和逐调用深拷贝，成本与缺省默认值大小相关，不修改原函数或通用校验器。

## 后果

签名成为 schema 的来源。安全元数据仍由作者如实声明。type 列表从静默忽略改为真正执行，是行为收紧。JSON Schema 可插拔校验与复杂模型支持留到后续 B6 扩展。

## 验证

test_tool_decorator 覆盖映射、默认值、绑定、拒绝路径、返回映射、审批/Gate、零超时与取消；test_api 验证导出身份。示例 34 和教程 33 导入正式包，默认完全离线。

默认值回归覆盖不匹配的 primitive/Literal/Optional/Annotated/嵌套列表、浮点转换、显式值优先、原对象/schema/原函数默认值修改、失败及取消后的再调用、双 Agent 并发隔离；schema 声明和 durable ToolCall 不因运行时默认绑定而积累状态。
