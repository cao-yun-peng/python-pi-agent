# ADR 0035：远端工具适配之前的通用接缝

状态：已接受。对应 MCP 开发方案阶段 0，关联 ADR 0014、0028、0033、0034。

## 背景

工具校验在 loop 内固定调用 JSON Schema 子集函数，远端 schema 的 anyOf/$ref/pattern 可能被忽略。PluginAPI 缺少借用观察端口；ToolExecutionContext 缺少 run/turn，跨任务适配器难以建立关联。超时也不能证明外部副作用未发生。

## 决定

1. ports.SchemaValidator 提供 supports 和同步 validate。AgentComponents.schema_validator、Profile factory 和 Builder.with_schema_validator 显式组装；SessionSpec 延用端口实例身份比较。loop 在审批之前通过该端口校验，对参数和 schema 传深拷贝，避免校验器偷换审批或执行输入。ToolValidationError 生成已有模型错误结果；配置错误原样传播；原生故障包装 InternalError 并保留 cause。
2. 默认 SubsetSchemaValidator.validate 包装已有函数，保持未知关键字被忽略的兼容行为；supports 保守声明完整支持范围，不自动替用户选择策略。JsonSchemaValidator 通过可选 [jsonschema] extra 使用 Draft 2020-12，显式本地 Registry，支持内部引用和应用预登记资源，不自动联网/读文件。仅支持明确的 dialect/vocabulary/关键字集合，format 为标注；错误仅含断言类别，不回显参数、路径或原始库错误。编译缓存每实例有界，schema 与资源独立快照。
3. PluginAPI.event_sink 只读地借用 PluginHost 已隔离的观察端口，不改变资源所有权；插件仍用 register_cleanup 释放自己的资源。观察失败不改变注册、运行或回滚结果。
4. ToolExecutionContext 末尾新增默认 None 的 run_id/turn_id，沿审批、Gate、handler 与结果 middleware 传递。turn 非空要求 run 非空；直接调用工具仍可缺省。工具外层超时提示副作用可能未知、不得自动重试；取消原样传播且不制造已完成 tool result。

## 备选方案

- 在远端适配器 execute 才完整校验：会让非法参数先经过审批，不能替代统一前置校验。
- 默认切换所有工具到完整校验：会改变已有手写 schema 行为，并使基础包依赖 jsonschema，不采用。
- PluginAPI 暴露 Store/Runtime 或私有 ContextVar：权限过宽或产生隐式身份来源，不采用。
- 增加 MCP 专属组件字段和主循环分支：上述缺口适用于所有远端工具，MCP 连接与协议实现留在后续适配层。

## 后果

基础 API 导入不需要可选依赖，完整校验必须显式选择；supports 是能力预检，不承诺任意 schema 或 CPU 隔离。同步校验不能由异步 timeout 抢占任意昂贵正则；外部 schema 还需要适配器的大小与复杂度预算。默认子集保留其历史语义，不能当作完整 Draft 2020-12 的替代品。

工具身份与插件事件仍是观察上下文，不新增 durable schema 或审计承诺。超时文本的变化写入 CHANGELOG。MCP discovery、owner task、传输、认证和 YAML capability 均未在本阶段实现。

## 验证

test_schema 验证默认兼容、完整断言、离线引用、非法 schema、可选依赖缺失、快照和脱敏错误；test_generic_tool_seams 验证审批前顺序、校验器替换、失败隔离、跨任务/跨 turn 身份、超时与取消。已有组件字段覆盖、SessionSpec 与 API 身份测试同步扩展。CI 安装 [otel,jsonschema] 执行离线测试；mypy 覆盖 schema 适配模块。示例 36 和教程 35 导入正式包。
