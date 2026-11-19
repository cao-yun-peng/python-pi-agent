# ADR 0034：类型化工具装饰器

状态：已接受。关联：路线图 B6、ADR 0002、0014。

## 背景

FunctionTool 要求分别维护函数、手写 schema 与返回包装，三者容易漂移。

## 决定

`tools/decorator.py` 用 async 函数签名推导 schema，产生显式交给 AgentComponents 或 PluginAPI 的 DecoratedTool，不注册全局对象。保留 fn 和 __wrapped__，执行仍经过既有校验、审批、Gate、超时与取消管线。

只接受 str/int/float/bool、同类型 Literal、list、开放 JSON object、Optional 和 Annotated 描述；ToolExecutionContext 注入而不暴露给模型。拒绝同步函数、可变参数、位置专用参数和复杂对象。默认值必须是有限 JSON 值，保存独立副本。返回 JSON 值映射为 ToolOutput，None 产生空文本。ToolError 是作者明确允许暴露的模型错误，普通异常继续由 loop 隐藏文本。

schema 只使用现有校验器执行的关键字。校验器增加 type 列表，并对 Optional 的非空 object/array 继续递归校验。description/default 仅为标注。

## 备选方案

默认引入 pydantic 增加依赖；完整 JSON Schema 会产生无法执行的约束；线程执行同步函数无法保证取消；装饰即全局注册违反显式组装原则。均不采用。FunctionTool 保留用于复杂手写契约。

## 后果

签名成为 schema 的来源。安全元数据仍由作者如实声明。type 列表从静默忽略改为真正执行，是行为收紧。JSON Schema 可插拔校验与复杂模型支持留到后续 B6 扩展。

## 验证

test_tool_decorator 覆盖映射、默认值、绑定、拒绝路径、返回映射、审批/Gate、零超时与取消；test_api 验证导出身份。示例 34 和教程 33 导入正式包，默认完全离线。
