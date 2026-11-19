# ADR 0039：YAML MCP capability、秘密引用与稳定 API

状态：已接受。完成 MCP 方案阶段 4，复用 ADR 0031/0035–0038。

## 背景

SDK 与 Tool 已可显式代码组装，接下来需要配置接入。YAML 不能成为自动启动任意进程或授予工具权限的依据，解析也不能顺便读取环境变量或建立连接。

## 决定

- `application/capabilities/mcp.py` 定义纯 McpCapabilitySettings/McpServerConfig 和 parse_mcp_settings。配置只接受显式工具白名单、transport 判别字段、固定预算与期限；stdio executable/cwd 相对配置目录规范化，args 原样保留。env/header 只保存变量引用，禁止内联凭据、Shell 求值与 transport 字段混用。
- 应用通过 mcp_capability_registration 显式扩展 CapabilityCatalog，并把同一 Catalog 交给 loader 和 Builder。默认 Catalog 仍只有 Bash，安装 extra 不自动注册或启动 MCP。配置与稳定 facade 导入均不需要 SDK。
- SecretResolver 为借用的同步窄协议。MappingSecretResolver 只保存应用明确挑选的秘密快照，没有自动 process-env/dotenv 搜索。初始化先解析所有 server 的全部引用并验证实际 HTTP 头，随后由 mcp_plugin 检查所有精确目标策略，再打开首个 endpoint。默认 resolver 为空、默认连接策略拒绝。
- 同一 SchemaValidator 显式传给注册函数和 Builder。工厂构造每 server 的 SDK options，复用正式 mcp_plugin；测试可注入 Fake connector。模块不新增 Builder 分支、全局注册或主循环特例；失败、取消、逆序回滚继续由 PluginHost/owner 负责。
- 将 SDK 无关 DTO/端口/错误、McpTool、SdkMcpConnector/options、注册函数、Settings 和 SecretResolver 放入稳定 api 与根包导出。实际 SDK/HTTP 类型保持适配层内部；Fake 仍从 testing 导入。

## 备选方案

顶层 mcp 配置键、默认自动发现、从 YAML 推导“信任全部”、复用模型 API key resolver、在 parser 读取环境/建立连接、把已解析凭据放进 Settings 或 durable log，都会扩大副作用与秘密边界，因此未采用。

## 后果

应用需要明确提供目标策略、指定秘密来源、共享校验器，并分别配置审批和 Gate。命令相对配置目录解析，不查找 Shell/PATH 或展开 args；使用受信任配置或自己审阅的绝对目标。生产环境可自行实现 SecretResolver/ConnectionPolicy，但不能把审批工具调用当作批准先前启动。自定义 SessionSpecResolver 也必须保留工具对应的完整校验器与策略；McpTool 再校验不能替代审批前验证。

## 验证

纯 parser 测试覆盖规范化、引用快照、未知字段、预算和默认目录不变；Fake Catalog 端到端证明 loader 无连接、缺 Catalog/秘密/策略时零连接、多 server 秘密全部预检和正常释放。缺 mcp/mcp_types/httpx2/jsonschema 的导入测试证明稳定 API 无隐式 extra。显式 YAML→真实 loopback SSE 集成验证凭据与 per-server 选项；示例 40 用 YAML 启动仓库 stdio fixture 和 FakeProvider，观察 opened/echo/closed。教程 38、README、知识地图及上游映射同步。
