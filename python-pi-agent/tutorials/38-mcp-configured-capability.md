# 38：从 YAML 组装 MCP 工具

本章解决应用部署边界：怎样让 agent.yaml 选择 MCP server 和工具，同时保持解析无副作用、秘密独立、权限明确、关闭可验证？正式实现位于 application/capabilities/mcp.py；它复用上一章 SDK owner 和前章 mcp_plugin，不产生第二套运行逻辑。

## 运行完整切片

```powershell
.venv/Scripts/python.exe -m pip install -e "python-pi-agent[mcp]"
.venv/Scripts/python.exe -B python-pi-agent/examples/40_mcp_yaml_tools.py
.venv/Scripts/python.exe -B -m unittest discover -s python-pi-agent/tests -p test_mcp_configuration.py
$env:PI_AGENT_MCP_INTEGRATION='1'
.venv/Scripts/python.exe -B -m unittest discover -s python-pi-agent/tests -p test_mcp_configuration_integration.py
```

示例生成临时 YAML，启动仓库自带 stdio server；Provider factory 明确返回 FakeProvider，没有真实模型费用。最后打印 fixture 的 opened → echo → closed 轨迹。配置单测用 FakeMcpConnector；显式集成只访问 loopback SSE，不访问外网。

## 配置形状

下列 capabilities 子树需要与已有 version/provider 配置组合：

```yaml
capabilities:
  mcp:
    servers:
      notes:
        transport: stdio
        command: ./bin/python.exe
        cwd: .
        args: [./servers/notes.py]
        env_refs:
          NOTES_TOKEN: NOTES_SERVICE_TOKEN
        tools: [search_notes]
      docs:
        transport: streamable_http
        url: https://docs.example.com/mcp
        header_refs:
          Authorization: DOCS_AUTHORIZATION
        tools: [search]
        protocol_mode: auto
        call_timeout_seconds: 30
        max_pending_requests: 64
        limits:
          max_result_bytes: 1048576
          max_structured_bytes: 131072
```

所有 tools 都是原始远端名称，必须明确列出且非空。command/cwd 相对配置文件所在目录规范化；args 不做变量、Shell 或任意路径展开，参数内部路径含义由 server 决定。command 是文件路径，不使用 Shell/PATH 查找或自动 npx/uvx 下载。stdio 与 HTTP 字段互斥；原始 env/headers、api_key 等内联秘密字段拒绝。

默认连接/发现期限 10 秒、调用期限 30 秒、cleanup grace 10 秒；owner 清理预算为 grace 的 80%，默认 8 秒。可配置 max_discovery_pages、max_pending_requests 以及 McpLimits 的正整数预算。HTTP 默认 HTTPS，本机明文需额外 allow_insecure_local: true；这不替代应用的目标策略。

## 显式装配

从 pi_agent_py.api 导入 mcp_capability_registration、MappingSecretResolver、AllowedMcpConnections 和 JsonSchemaValidator。应用创建同一个 validator，传给注册函数和 Builder。它创建扩展后的 CapabilityCatalog，分别交给 load_app_config 与 AgentRuntimeBuilder.from_loaded_config；省略任意一端都不能识别 mcp。默认目录仍只有 Bash，pip 安装不会自动开启能力。

SecretResolver.resolve(name) 只返回被明确请求的单个值。MappingSecretResolver 接收应用挑选的字典快照；例如应用在自己的配置边界读取 `os.environ["DOCS_AUTHORIZATION"]`，再提供这一个键。不要传完整 os.environ。需要 dotenv 时由应用显式选文件并筛选键；MCP 不搜索父目录、不修改进程环境，也不复用模型 Provider 的 api_key_override。

AllowedMcpConnections 接收应用独立审阅的完整目标 spec，包含精确 executable/argv/cwd 或 URL 及允许注入的凭据。不要把不可信 YAML 解析结果直接全部放进 allowlist；示例的 YAML 和目标均由应用代码生成，属于明确受信任 fixture。不同主体应使用独立 Runtime/连接。

初始化按顺序执行：

1. parser 生成非秘密 Settings，没有连接或秘密读取。
2. 工厂解析所有 server 的引用，并验证实际 HTTP 头；缺任意引用时首个 server 也不启动。
3. mcp_plugin 检查所有精确目标策略，再连接、发现、预检并暂存工具。
4. 模型 ToolCall 仍经过完整输入校验、ApprovalPolicy、Gate、单次调用、结果投影/B5 和 durable 提交。
5. Runtime 关闭或构建失败时，Host 逆序释放 owner 和连接。

连接许可、调用审批和 external Gate 是三个独立决定。YAML 不自动授权三者；StaticApprovalPolicy(True) 只是示例替身。自定义 SessionSpecResolver 时，返回的组件也应保留同一个完整校验器；不要因为 McpTool 有防御校验就省略审批前的统一验证。

## 失败与掌握标准

先不提供 secret，验证 build 失败且 Fake connector.opens 为空；再补 secret，省略连接策略，仍应零连接。让第二台 server 缺一个引用，证明第一台没有提前启动。发现缺少选中工具时，允许已经连接，但发布失败并关闭 endpoint。取消行为继续由 owner/Session 处理，历史恢复不发送旧工具请求。

能从 YAML 追到 schema、目标策略、秘密引用、审批/Gate、真实请求、变换后的 durable 结果以及释放证据，才算完成本章。可以替换 SecretResolver、Connector、SchemaValidator 和 Store，而不用改 Agent loop。

Pi 固定 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 McpServerRegistry 对应配置到工具目录的职责；Python 使用既有 CapabilityCatalog 显式扩展，不复制用户/项目多层配置、交互重连、OAuth、资源工具化或动态 exposure。DeepSeek Harness 只映射 capability 的 Definition/Provider/Consumer/Lifecycle/Evidence 思想。决策见 [ADR 0039](../docs/adr/0039-configured-mcp-capability.md)。
