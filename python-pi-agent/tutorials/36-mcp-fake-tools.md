# 36：MCP Fake 端到端切片

本章要解决的问题：如何把远端发现的工具变成 Harness 已有的 Tool，并证明权限、结果提交与取消语义保持完整？它发生在应用 build 时的插件注册，以及每轮模型输出 ToolCall 后的普通工具执行阶段。

## 运行

在仓库根目录、已安装项目的虚拟环境中执行：

```powershell
.venv/Scripts/python.exe -m pip install -e "python-pi-agent[jsonschema]"
.venv/Scripts/python.exe -B python-pi-agent/examples/37_mcp_fake_tools.py
.venv/Scripts/python.exe -B -m unittest discover -s python-pi-agent/tests -p test_mcp.py
```

安装依赖需要包源；示例与测试本身完全离线，没有子进程 server、网络请求或付费模型。Windows 受限执行环境的 asyncio/Temp 约束见根 AGENTS.md。

## 从发现到续轮

正式实现是 `tools/mcp/contracts.py`、`tool.py` 和 `plugin.py`。测试替身在 `pi_agent_py.testing`。阶段 1 使用定义模块入口；后续阶段才接 YAML 和稳定 API facade。

```mermaid
sequenceDiagram
    participant App as Builder/PluginHost
    participant MCP as FakeMcpEndpoint
    participant Loop as Agent loop
    participant Store as SessionStore
    App->>App: 检查全部目标策略（默认拒绝）
    App->>MCP: open → 立即登记 cleanup → list_tools
    App->>App: allowlist/schema/预算/冲突检查 → 发布普通 Tool
    Loop->>Loop: 输入校验 → 审批 → Gate
    Loop->>MCP: call_tool(原名, 参数快照)，只提交一次
    MCP-->>Loop: 规范 DTO
    Loop->>Loop: 输出校验/预算/投影 → B5 middleware
    Loop->>Store: 提交最终 ToolOutput
    Loop->>Loop: FakeProvider 读取工具结果并续轮
    App->>MCP: 关闭/构建失败时逆序释放
```

把同一个 `JsonSchemaValidator()` 显式传给 `Builder.with_schema_validator()` 与 `mcp_plugin(schema_validator=...)`。前者保证审批前校验；后者预检远端 schema，并校验原始输入和输出。默认子集的 supports 会拒绝它无法完整支持的关键字，不会默默接受复杂 MCP schema。

连接授权、人工审批和执行 Gate 是三个独立决策。`AllowedMcpConnections((spec,))` 允许连接该精确目标；`StaticApprovalPolicy(True)` 是示例的固定审批替身；`DeclaredSideEffectGate` 需要显式允许 external 和选定本地名称。生产应用应换成自己的策略。远端 readOnlyHint 不会改写本地 external/approval/retry/replay 声明。

Spec 使用正式的 stdio/HTTP 数据形状，但 Fake 只按 server_id 返回脚本 endpoint，command 从不执行。不要用此示例判断某个本地 server 已经被沙箱隔离。

## 观察执行轨迹

示例打印事件名与 run/turn，不打印事件正文。一条成功轨迹的关键节点是：

```text
mcp_connection_started → mcp_tools_discovered → mcp_connection_ready
message_committed (assistant ToolCall)
tool_approval_decided
mcp_request_started → mcp_request_finished
tool_result_transformed → message_committed (tool)
message_committed (assistant answer)
mcp_connection_closed
```

连接阶段没有 run/turn；请求阶段使用 ToolExecutionContext 的显式身份，每次另生成 invocation/span ID。同一 ToolCall.id 在不同 Session 出现也不会合并请求。事件是 best-effort 观察，恢复依赖 Store；OTel 仅拿内容为空的投影，不把远端名称变成自由指标标签。

## 结果为什么要封装

工具名使用稳定摘要，既避免 `a/b` 与 `a:b` 规范化后重名，也不依赖发现顺序。McpTool 始终使用绑定的原名发送请求，模型不能通过本地工具名选择任意远端方法。

输出的 structured_content 形状为：

```json
{"mcp":{"server_id":"notes","remote_tool":"notes/search","schema_digest":"…","protocol_version":"2025-11-25","has_structured_content":true,"omitted_content_blocks":0},"result":{"count":2}}
```

output_schema 校验的是 `result` 的远端原值，成功响应缺少承诺的 structuredContent 也会失败。`has_structured_content=true, result=null` 与缺失结构化值有不同含义。文本按原顺序连接；混合 image/audio/resource 块只保留类型提示与数量，不保存二进制，也不自动追踪 resource_link。

结果预算在 B5 之前检查。文本截断只影响 content，不会缩小结构化数据；应用需要脱敏时，可以像 JSONL 测试那样让结果 Hook 返回新的 content 和 structured_content。内存日志、JSONL、下一次模型请求与 fork 看到的都是变换后的结果。

## 失败和取消

示例第二次调用抛出脚本化 McpTransportError，得到固定的 outcome_unknown，提醒不要自动重试。成功响应的 output schema 错误、超预算、只有不支持内容、input_required 与内部期限到达同样如此：远端可能已发生副作用。每个 ToolCall 最多一次 endpoint 调用，没有自动续答或重发；这不承诺模型未来不会主动生成新调用，审批策略仍需参与。

外部 CancelledError 原样传播，不伪造已完成结果。测试用 `WaitUntilCancelled()` 和 Event 精确停在发现或调用阶段，取消后验证连接释放，以及下次打开会话由既有恢复逻辑补写中断错误。恢复、重启读取与 fork 都不发送旧请求。

多个 server 全部是必需项。后一个 server 发现失败时，Host 不发布暂存工具，按后开先关释放所有已打开 endpoint；本地名称冲突同样回滚。Connector 尚未返回的部分启动由 Connector 自己负责清理。

## 练习与掌握标准

1. 移除连接白名单、审批或 external Gate 中任意一个，解释为何对应边界被拒绝，并断言远端调用数为零。
2. 把成功结果的 count 改成字符串，观察 unknown；再将 is_error 设为 true，解释业务错误为何不要求满足成功 output_schema。
3. 用 WaitUntilCancelled 阻塞第一次调用，同时启动第二个 Session，取消第一项，证明第二项仍能完成且 invocation ID 不同。
4. 比较原始响应与 JSONL，只允许结果 Hook 变换后的数据进入 durable log；关闭 endpoint 后仍能 load/fork。

能解释并离线复现以上路径，才算掌握本阶段。下一阶段实现 SDK owner 与真实 stdio，另外验证 enter/exit 同任务、分页和子进程清理；HTTP/SSE 原始接收预算、有限队列和凭据解析尚未具备。当前检查的是已解析 DTO，不能保护解析前内存，也不对任意恶意 schema 提供 CPU 隔离。

## 对照与取舍

Pi 固定 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 `packages/coding-agent/src/core/mcp-servers.ts::McpServerRegistry` 与 `packages/coding-agent/docs/mcp.md` 对应“远端目录转本地工具”。Python 复用自己的显式插件事务与 Tool 端口，未复制 Pi 产品配置、搜索、OAuth、重连和动态 exposure。对 DeepSeek Harness 只映射 Definition/Provider/Consumer/Lifecycle/Evidence 的能力接缝思想，不声称存在未经核对的 MCP 同名实现。完整决策见 [ADR 0036](../docs/adr/0036-mcp-fake-tool-slice.md)。
