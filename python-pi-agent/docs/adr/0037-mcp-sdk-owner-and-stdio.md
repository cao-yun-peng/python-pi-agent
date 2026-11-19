# ADR 0037：MCP SDK owner 与 stdio

状态：已接受；实现 MCP 方案阶段 2，延续 ADR 0036 的 Tool 与插件边界。

## 背景

Builder、run、close 可来自不同任务，不能让它们分别进入和退出 SDK 的 AnyIO cancel scope。Fake Lock 也不能证明真实进程会退出。核对版本为 PyPI 官方 `mcp==2.3.0`、`mcp-types==2.3.0`；接口以安装包源码为准。

## 决定

- `OwnedMcpEndpoint` 用独立、空 Context 创建的 owner 任务持有整个 SDK 异步上下文；内部 TaskGroup 跟踪请求子任务。并发执行一项，待执行队列默认最多 64 项；排队计入调用期限。取消只取消对应请求并等待本地清理，未提交的请求可从队列移除。满队列、关闭或失效连接返回明确 not_started。
- `SdkMcpConnector` 延迟导入 SDK，`[mcp]` 固定 2.3.0。Client 只负责 transport/协商；工具列表与调用通过 `ClientSession.send_request` 单次提交，避免 SDK 的列表过滤、参数 header 缓存、输出 schema 校验和高层 input-required/HeaderMismatch 重发。成功输出仍由应用选择的 SchemaValidator 校验。
- 完整分页发现检查 cursor 循环、重复原名、页数、工具数、单 schema 与累计目录预算。保留原 schema，不启用 x-mcp-header；选中 schema 的扩展继续由 supports 拒绝。已收到的 list_changed、协议错误或传输错误使当前快照失效，后续必须重建；没有自动重新发现、重连或重放。
- stdio 精确 executable/argv，不经过 Shell、不下载 server。stderr 显式送入 devnull；SDK 固定日志名称使用显式注册、按 owner Context 生效、退出时撤销的过滤器，防止库异常/正文绕过自己的安全事件管线。其他任务日志不受影响。凭据仅使用 spec.env；SDK 仍继承其基础环境白名单。
- owner 默认清理期限 8 秒，插件/Tool 的 cleanup_grace 默认由 5 调整为 10 秒，为固定 SDK 的多步关闭/强杀/回收留出空间。SDK 的平台实现负责真实进程树；Harness 不自行 taskkill 或 Shell 杀进程。清理失败按本地分类报告且保留 cause。

## 备选方案

直接在调用方持有 AsyncExitStack 会跨任务退出 cancel scope；只取消 Future 会使 SDK 请求无人管理；使用高层 call_tool 可能自动重发；手写 JSON-RPC 客户端会复制 SDK 协议与取消实现。均未采用。

## 后果

基础包和 MCP 定义模块无需 SDK；开启能力才需要 extra。一个 Runtime 仍对应一个主体与固定凭据。接收预算目前针对解析后的 DTO；stdio 行缓冲、HTTP JSON/SSE 原始读取保护不是本阶段承诺。HTTP 在阶段 3 开放。队列关闭与取消有界；无法强制终止任意恶意 Python 协程，同样不提供本地主机进程沙箱。

## 验证

`test_mcp_connection.py` 验证同任务 enter/exit、独立取消、有界队列、未就绪取消和清理失败；`test_mcp_sdk.py` 脚本化验证分页、完整 schema、presence、失效与单次发送。显式 `PI_AGENT_MCP_INTEGRATION=1` 的 `test_mcp_stdio.py` 使用仓库 fixture 和 FakeProvider 验证 modern/legacy、真实取消、断连与跨任务生命周期；Windows 强制关闭注入零 SDK grace，提前持有父/子进程句柄，以 OS 退出信号证明进程树终止。默认测试跳过外部进程集成。示例 38、教程 37 导入正式客户端。

参考：[固定 SDK stdio](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/client/stdio.py)、[Session](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/client/session.py)、[Client](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/src/mcp/client/client.py)。
