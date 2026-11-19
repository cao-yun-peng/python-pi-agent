# ADR 0038：Streamable HTTP 与接收预算

状态：已接受；MCP 阶段 3，延续 ADR 0036/0037 的工具与 owner 接缝。

## 背景

解析后大小检查不能阻止 SDK 缓冲巨大 JSON/SSE。固定 SDK 2.3.0 使用独立的 httpx2，默认还会跟随同源重定向、恢复 SSE；其 POST 子任务也不会把所有自定义 transport 异常转为请求错误，需要用兼容的失败通道接线。

## 决定

- SDK 延迟创建并拥有独立 httpx2.AsyncClient/AsyncHTTPTransport，保持 TLS 校验、关闭环境代理/认证继承、重试和自动重定向。默认 HTTPS；明文本机开发目标须同时满足显式 loopback 许可与精确连接策略。拒绝 URL 控制字符和保留协议/HTTP header，凭据仅为显式 spec.headers。
- `_http.BoundedHttpTransport` 在 SDK 解码前检查声明长度与实际读取字节；每个 JSON 或 SSE 响应流均受 max_result_bytes 限制，SDK 另有同值的单 SSE event 上限。发送 Accept-Encoding: identity 并拒绝压缩响应，避免解析前解压绕过预算。解析后的结构化值、深度、文本及目录预算继续生效。
- 拒绝全部重定向；Last-Event-ID 恢复请求在网络前拒绝，不重发 tools/call。SDK 可能在内部尝试恢复退避，但不会发生恢复网络访问；总调用期限仍有效。旧协议初始化所需的 GET/DELETE 由 SDK 管理。
- 认证拒绝、重定向、声明超限和连接失败保存本地分类，关闭原响应，并返回固定的 HTTP 400 JSON-RPC error（id=null，由 SDK 关联原请求）。实际流中断/超限转换为 SDK 可处理的 StreamError。SDK endpoint 从本地 fault 恢复 MCP 错误分类；异常原链保留在内存，模型只看到固定 unknown。这样不会因请求失败破坏 SDK task group，再把同一错误当作关闭失败。
- 显式要求 2026-07-28 时仍通过真实 auto discovery 协商，再检查版本；不使用没有真实 server capabilities 的合成 adopt 结果。

## 备选方案

只截断 ToolOutput 无法保护原始缓冲；复用模型 httpx client 与 SDK 类型不兼容且会混淆所有权；允许库自动跳转/恢复会扩大授权目标与行为；从 transport 直接抛任意领域异常会终止 SDK task group。均未采用。

## 后果

预算限制解码前接收和解析后结果，不承诺任意 schema 的 CPU 隔离，也不能阻止 HTTP 库分配单个传入 chunk。压缩、自动重连/恢复、OAuth 仍未开放。stdio 仍只有解析后预算，不把 HTTP 保证扩展到 stdio 行缓冲。一个 Runtime 的连接仍面向同一主体；凭据解析和 YAML 属于阶段 4。

## 验证

`test_mcp_http.py` 使用字节流证明 JSON/SSE 在消费超预算 chunk 后停止、声明过大/压缩/401 不读取原文、流与 transport 释放、恢复不访问网络。显式 `test_mcp_http_integration.py` 仅绑定 loopback，覆盖 modern/legacy、JSON/SSE、显式头、401、超大结果、断连、重定向、带 event ID 的中断及 client 关闭；每次工具请求只调用一次，unknown 后关闭正常。示例 39 使用官方 SDK server 和 FakeProvider，一键本地运行。第 37 章扩充 HTTP 机制和练习。
