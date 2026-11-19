# ADR 0033：请求投影与工具结果的 Middleware 链

状态：已接受。关联：B5，ADR 0012、0014、0019、0028、0034。

## 背景

standalone Agent 缺少可组合的请求注入点，工具结果在写入日志之前缺少统一的截断和脱敏点。NextTurnHook 改变 canonical history，SessionPlugin 有 Session 实例生命周期，不能混为同一能力。

## 决定

runtime/middleware.py 定义冻结的 Middleware 值和私有执行器。provider_request 复用 ProviderRequestHook，tool_result 使用 ToolResultHook 与领域值 ToolResultContext。每阶段仅顺序变换值，按 priority 和 tuple 位置升序调用。输入与输出深拷贝；每项有独立可选 asyncio.timeout、契约检查和 started/terminal span，观察 payload 不含正文。

执行顺序是 ContextStrategy → 请求 middleware → SessionPlugin → context_prepared → Provider。投影只在当前请求中存在，不写 canonical history。失败或非法返回抛 MiddlewareError，保留 cause，run_failed 类别为 middleware；不泄漏原始异常文本。压缩摘要的 Provider 调用不经过此链。

工具结果链位于 _run_tool 返回与 tool message 提交之间，ToolResultContext 保留执行前身份快照、原 outcome、run/turn。覆盖 success/error/timeout/execution_denied/sandbox_timeout/sandbox_error；未知工具、校验失败、审批拒绝、length 未执行调用和恢复补写结果不经过此链。返回必须是 ToolOutput 且保持 is_error。链失败立即停止后续变换，提交固定的“工具已执行但结果未知，不得自动重试”错误与失败名称、原 outcome。原输出不持久化；外部取消直接传播，不提交结果，由既有恢复机制补写 unknown。

AgentComponents 持有冻结 tuple 并拒绝重名；Profile 的 factory 显式构造；Builder.with_middleware 替换 Profile 默认并跳过其 factory；插件通过可撤销注册 staging、冲突检查后原子发布，Builder 将插件项追加在组件项之后。SessionSpec 按每项实例身份比较。Middleware 无独立 close，资源由应用管理，插件资源走 register_cleanup。发布的组件快照不热更新。

内置 TruncateToolResults 支持 head/tail/head_tail，字符预算包含截断标记，保留 structured_content/is_error。计数与 Shell 字节预算不同；结构化数据仍可能很大，须另加变换。

## 备选方案

不使用洋葱式 call_next，避免跳过安全管线。不采用可选方法的大 Protocol，避免拼写错误变成隐式缺失。不合并 NextTurnHook 与 SessionPlugin。不在结果处理失败后提交原文，也不让 run 直接失败而丢失“副作用已发生”的模型提示。

## 后果与后续

RAG 可以在 standalone/Session/Runtime 共享使用，工具结果变换成为 durable fact。请求变换必须保持工具配对及供应商的 system 消息布局，v1 不自动校验这两点。run_start 涉及输入来源标注，assistant_message 涉及校验重试与请求预算，tool_call 涉及审批快照，三者推迟到独立 ADR。配置驱动中间件复用 PluginAPI，无导入注册副作用。

## 验证

test_middleware、test_result_truncation 覆盖排序、透传、深拷贝、失败、零超时、取消恢复、durable/replay/fork 和 scope；downstream_middleware 只从 api 导入并验证 Builder/Plugin 两条路径；既有字段覆盖测试守护组件接线。test_otel 覆盖 success/error/cancelled span，事件测试覆盖脱敏与无正文。示例 35、第 34 章导入正式包离线复现。
