# 第 34 章：请求投影与工具结果的顺序变换

RAG 需要每次请求注入资料，长工具输出需要在进入日志之前截断。这两个阶段都变换值，但只有后者改变 durable fact。

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\35_middleware.py
```

示例导入正式包，用假检索器、装饰工具和 FakeProvider 跑两轮。观察 middleware_started → middleware_finished 的相同 span_id，以及 tool_result_transformed 中的名称、changed 和原/最终字符数。对照请求与 Store 快照：检索资料只在请求里，截断后的结果保存在日志里，重放、fork 和后续请求都读取该结果。

## 选择接缝

| 需求 | 接口 | 状态与生命周期 |
|---|---|---|
| 按 token budget 选择完整历史 | ContextStrategy | 输出 ContextPlan，不改历史 |
| 请求注入、过滤、工具结果截断 | Middleware | 可供 standalone 使用；由创建者释放资源 |
| 每 Session 独立状态和关闭逻辑 | SessionPlugin | Host 创建 lease，显式关闭 |
| 安全边界的 durable compaction | NextTurnHook | Session 控制持久化与 replay |
| 展示、日志和遥测 | EventSink | best-effort 观察，不能改变执行 |

## 组装与顺序

Middleware 声明 provider_request、tool_result 中至少一个阶段。AgentComponents.middleware 保存 tuple，Profile.middleware_factories 显式创建各项。Builder.with_middleware 替换 Profile 默认；插件用 PluginAPI.register_middleware 返回撤销句柄，并用 register_cleanup 管理资源。Builder 把插件项追加到组件项之后；重名会拒绝整个组装。注册撤销和卸载影响后续快照，已有组件不会热更新。

每阶段按 (priority, tuple 位置) 升序，前项输出交给后项。请求的完整顺序为 ContextStrategy → middleware → SessionPlugin → context_prepared → Provider，因此 selected_message_count 是最终计数。每次工具续轮都重新执行；需要 run 内缓存时由实现按 context.run_id 管理。压缩摘要直接调用 Provider，**不经过请求 middleware**，不能假设同一脱敏逻辑覆盖摘要。

请求 middleware.prepare(context, plan) 必须返回 ContextPlan 并保留 source_message_count，通常用 dataclasses.replace(plan, messages=...) 创建新投影。示例把资料放在 leading system 消息之后，避免拆开 tool call/result。供应商布局与配对仍由实现者保证，v1 不自动检查。

## 两种失败策略

请求链失败会停止 run，抛 MiddlewareError，原异常只在 cause 中保存。失败的过滤或注入不能把未处理的请求继续交给模型。

结果链在工具执行之后运行，收到 ToolResultContext.execution、outcome、run/turn。它必须返回 ToolOutput，且保持 is_error。处理失败后不会回退原文，也不会自动重试工具；runtime 提交固定错误，明确“结果未知，副作用可能已发生”，模型仍能继续。structured_content 保存 middleware_failed、失败名称和原 tool_outcome。取消不生成 tool message；重开会话后由已有恢复逻辑补写中断结果。

只有 _run_tool 返回的成功、工具错误、超时、Gate 拒绝和沙箱错误经过此链。未知工具、schema 错误、审批拒绝、length 未执行调用与恢复文本由 runtime 直接生成，不经过结果变换。中间件无法跳过审批或执行，因而使用顺序变换而不用 call_next 洋葱模型。

每次变换接收深拷贝，返回值也保存独立副本。timeout_seconds 使用 asyncio.timeout；测试用零超时和可取消的 Event 等待，无真实计时依赖。异常、超时、取消均有配对 capability span，payload 不含消息正文、参数或工具输出。安全日志仍应使用 RedactingEventSink。

## 截断和持久化的取舍

TruncateToolResults(max_chars, keep) 按字符处理 content，max_chars 至少 64，支持 head/tail/head_tail，标记本身计入预算，标记报告实际省略字符数。它保留 structured_content 和 is_error，**不会限制结构化数据大小或脱敏其中内容**；需要时再组合专用变换。Shell 的每流预算按字节，是另一个边界。完整输出需要外部存储时，由实现保存并在 structured_content 中留下引用，不能指望从截断后的日志恢复原文。

run_start、assistant_message、tool_call 三个阶段分别涉及用户来源、校验重试预算和审批参数快照，留给后续独立设计。练习：实现一个不改变 outcome 的结果脱敏器，证明原文未进入 durable log，并验证失败及取消路径。
