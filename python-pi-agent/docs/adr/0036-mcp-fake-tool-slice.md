# ADR 0036：MCP Fake 端到端切片

状态：已接受。实现 [MCP 方案](../design/mcp-client-tool-adapter.md)阶段 1，依赖 ADR 0035、0033、0014。

## 背景

通用工具接缝已经完成，需要先证明远端发现可以进入既有校验、审批、Gate、结果 middleware 和 durable 续轮，再引入 SDK、子进程和网络。否则连接问题与工具语义问题会混在一起，也无法用离线测试独立验证。

## 决定

1. 在 `tools/mcp/contracts.py` 定义不含 SDK 类型的 ConnectionSpec、ServerSettings、Limits、ToolDescriptor、Content、CallResult，以及 Connector、Endpoint、ConnectionPolicy 协议。Spec 是已解析的 stdio/HTTP 目标；Fake 不执行目标。显式 null 与缺少 structuredContent 用 presence 标志区分，schema、注解、凭据和结果使用独立快照。非文本 DTO 仅携带类型省略标记，不携带 base64、资源正文或原始 `_meta`。
2. `mcp_plugin` 在连接前检查全部目标的策略，默认全拒绝。发现完整目录后，仅选择显式原名 allowlist，验证目录/单 schema 预算和校验器支持，再暂存普通 Tool。每个 endpoint 返回后立即注册 cleanup，Host 在冲突、失败或取消后逆序回滚。关闭幂等且有本地宽限期限；Connector 自己负责未成功返回的部分启动。
3. 本地名称固定为 `mcp__<server_id>__<slug>__<digest12>`，摘要输入是 server_id 与原名的规范 JSON，不依赖目录顺序；最长 64 个 ASCII 字符，保留原名用于实际调用。摘要或本地工具冲突都拒绝构建。所有 MCP Tool 固定 external、要求审批、不可安全重试或重放；远端 annotations 不能降低这些声明。
4. 同一 SchemaValidator 由应用同时交给 Builder 和插件。输入在通用 loop 审批前校验，McpTool 再防御性校验；输出仅校验成功结果的远端原值。用带独立 `$id` 的嵌套 schema resource 包装成对象，适配现有对象校验端口，同时保留原 schema 的本地 `$ref` 根。
5. McpTool 单次提交、不重试。文本按顺序连接，混合非文本块加入类型提示与省略数量，结构化结果带固定 mcp 元信息封装。不可表示、超预算、输出校验失败、已分类传输错误、内部超时均返回固定 outcome_unknown。取消原样传播，没有完成结果，由既有 Session 恢复处理未决调用。
6. 通过借用的隔离 EventSink 发出连接与请求事件，OTel 明确映射 span 配对。事件不包含地址、凭据、参数、输出或原始异常文本；请求携带显式 session/run/turn、call 与独立 invocation 身份。结果只经既有 B5 变换后进入 Store，replay/fork 不调用 endpoint。
7. `testing.FakeMcpConnector/FakeMcpEndpoint` 提供脚本、Event 阻塞点、调用记录与幂等关闭；endpoint 使用局部 Lock 验证串行执行和取消隔离。阶段 1 从定义模块导入，稳定 facade 和 YAML CapabilityCatalog 接线仍按阶段 4 推进。

## 备选方案

- 直接让 Agent loop 调用 SDK：运行核心会依赖具体协议，且无法独立证明生命周期和审批顺序。
- 先写 Fake 专属 transport：会污染生产目标契约；Fake 应消费正式的纯数据 spec。
- 根据 readOnlyHint 自动放宽权限：远端描述不是本地授权依据。
- 截断 middleware 代替结果预算：结构化字段和解析前内存都不会由文本截断保护。
- 把连接写入会话日志并恢复重试：可能重复副作用，连接与凭据也不属于模型可见 durable state。

## 后果与边界

无需 MCP SDK 或网络就能验证完整工具机制；复杂 schema 示例仅需已有 [jsonschema] extra。阶段 1 的预算针对已解析的规范 DTO，不是 HTTP/SSE 原始读取保护。CallResult 构造时还有固定的 1 MiB/64 层结构化规范化上限，ServerSettings 可进一步收紧；目录和结果的可配置预算在发布/提交前执行。同步 schema 校验没有 CPU 隔离保证。

尚未实现 SDK owner task、stdio/HTTP I/O、分页聚合、wire 接收预算、队列容量、凭据解析、自动重连、OAuth 或动态工具目录。Fake Lock 不能证明 SDK enter/exit 同任务或子进程无泄漏；这些是阶段 2/3 的独立验收。当前 Runtime 连接共享仅适合由应用确定的同一权限主体。

## 验证

`tests/test_mcp.py` 覆盖 FakeProvider 续轮、审批/Gate 零调用、复杂输入和输出引用、null/缺失、非文本和 input_required、预算、冲突、逆序回滚、发现取消、内部超时、并发取消隔离、JSONL 变换后提交及无连接恢复/fork。`test_otel.py` 验证 success/error/cancelled span；`test_schema.py` 阻断可选包导入，证明普通 API 与 MCP 定义模块不需要 SDK。新增模块纳入严格 mypy；[示例 37](../../examples/37_mcp_fake_tools.py)和[第 36 章](../../tutorials/36-mcp-fake-tools.md)导入正式实现。
